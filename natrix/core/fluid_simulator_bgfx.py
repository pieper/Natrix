from math import ceil

import numpy as np
# noinspection PyUnresolvedReferences
from bgfx import as_void_ptr, bgfx

from natrix.core.common.constants import TemplateConstants
from natrix.core.utils.shaders_utils import (
    create_point_buffer,
    load_shader, create_vector2_buffer)


class FluidSimulator:
    VELOCITY_READ = 0
    VELOCITY_WRITE = 1

    PRESSURE_READ = 0
    PRESSURE_WRITE = 1

    _num_cells = 0
    _num_groups_x = 0
    _num_groups_y = 0
    _width = 512
    _height = 512

    _speed = 500.0
    _iterations = 50
    _dissipation = 1.0
    _vorticity = 0.0
    _viscosity = 0.1

    simulate = True

    def __init__(self, width: int, height: int):
        self.vertex_decl = bgfx.VertexDecl()

        self.vertex_decl.begin() \
            .add(bgfx.Attrib.TexCoord0, 4, bgfx.AttribType.Float) \
            .end()

        self._create_uniforms()

        self._load_compute_kernels()
        self._set_size(width, height)
        self._create_buffers()
        self._init_compute_kernels()

    @property
    def speed(self):
        return self._speed

    @speed.setter
    def speed(self, value):
        if value > 0:
            self._speed = value
        else:
            raise ValueError("'Speed' should be greater than zero")

    @property
    def iterations(self):
        return self._iterations

    @iterations.setter
    def iterations(self, value):
        if value > 0:
            self._iterations = value
        else:
            raise ValueError("'Iterations' should be grater than zero")

    @property
    def dissipation(self):
        return self._dissipation

    @dissipation.setter
    def dissipation(self, value):
        if value > 0:
            self._dissipation = value
        else:
            raise ValueError("'Dissipation' should be grater than zero")

    @property
    def vorticity(self):
        return self._vorticity

    @vorticity.setter
    def vorticity(self, value):
        if value >= 0:
            self._vorticity = value
        else:
            raise ValueError("'Vorticity' should be grater or equal than zero")

    @property
    def viscosity(self):
        return self._viscosity

    @viscosity.setter
    def viscosity(self, value):
        if value > 0:
            self._viscosity = value
        else:
            raise ValueError("'Viscosity' should be grater than zero")

    def add_velocity(self, position: tuple, velocity: tuple, radius: float):
        bgfx.setUniform(self.position_uniform,
                        as_void_ptr(np.array([position[0], position[1], 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.velocity_uniform,
                        as_void_ptr(np.array([velocity, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.radius_uniform,
                        as_void_ptr(np.array([radius, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))

        bgfx.dispatch(0, self._add_velocity_kernel, self._num_groups_x, self._num_groups_y, 1)
        self._flip_velocity_buffer()

    # position in normalised local space
    # radius in world space
    def add_circle_obstacle(self, position: tuple, radius: float, static=False):
        bgfx.setUniform(self.position_uniform,
                        as_void_ptr(np.array([position[0], position[1], 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.radius_uniform,
                        as_void_ptr(np.array([radius, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.static_uniform,
                        as_void_ptr(np.array([1 if static else 0, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))

        bgfx.dispatch(0, self._add_circle_obstacle_kernel, self._num_groups_x, self._num_groups_y, 1)

    # points in normalised local space
    def add_triangle_obstacle(self, p1: tuple, p2: tuple, p3: tuple, static=False):
        bgfx.setUniform(self.p1_uniform,
                        as_void_ptr(np.array([p1[0], p2[1], 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.p2_uniform,
                        as_void_ptr(np.array([p2[0], p2[1], 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.p3_uniform,
                        as_void_ptr(np.array([p3[0], p3[1], 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.static_uniform,
                        as_void_ptr(np.array([1 if static else 0, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))

        bgfx.dispatch(0, self._add_triangle_obstacle_kernel, self._num_groups_x, self._num_groups_y, 1)

    def update(self, time_delta: float):
        if self.simulate:
            self._update_params(time_delta)

            # Init boundaries
            bgfx.dispatch(0, self._init_boundaries_kernel, self._num_groups_x, self._num_groups_y, 1)

            # Advect
            bgfx.dispatch(0, self._advect_velocity_kernel, self._num_groups_x, self._num_groups_y, 1)
            self._flip_velocity_buffer()

            # Vorticity confinement 1 - Calculate vorticity
            bgfx.dispatch(0, self._calc_vorticity_kernel, self._num_groups_x, self._num_groups_y, 1)

            # Vorticity confinement 2 - Apply vorticity force
            bgfx.dispatch(0, self._apply_vorticity_kernel, self._num_groups_x, self._num_groups_y, 1)
            self._flip_velocity_buffer()

            # Viscosity
            if self.viscosity > 0.0:
                for _ in range(self.iterations):
                    bgfx.dispatch(0, self._viscosity_kernel, self._num_groups_x, self._num_groups_y, 1)
                    self._flip_velocity_buffer()

            # Divergence
            bgfx.dispatch(0, self._divergence_kernel, self._num_groups_x, self._num_groups_y, 1)

            # Clear pressure
            bgfx.setBuffer(TemplateConstants.GENERIC.value, self._pressure_buffer[self.PRESSURE_READ],
                           bgfx.Access.WRITE)
            bgfx.dispatch(0, self._clear_buffer_kernel, self._num_groups_x, self._num_groups_y, 1)
            bgfx.setBuffer(TemplateConstants.PRESSURE_IN.value, self._pressure_buffer[self.PRESSURE_READ],
                           bgfx.Access.READ)

            # Poisson
            for _ in range(self.iterations):
                bgfx.dispatch(0, self._poisson_kernel, self._num_groups_x, self._num_groups_y, 1)
                self._flip_pressure_buffer()

            # Subtract gradient
            self._subtract_gradient_kernel.run(
                self._num_groups_x, self._num_groups_y, 1
            )
            self._flip_velocity_buffer()

            # Clear obstacles
            bgfx.setBuffer(TemplateConstants.GENERIC.value, self._obstacles_buffer, bgfx.Access.WRITE)
            bgfx.dispatch(0, self._clear_buffer_kernel, self._num_groups_x, self._num_groups_y, 1)
            bgfx.setBuffer(TemplateConstants.OBSTACLES.value, self._obstacles_buffer, bgfx.Access.WRITE)

    def _set_size(self, width: int, height: int):
        group_size_x = TemplateConstants.NUM_THREADS.value
        group_size_y = TemplateConstants.NUM_THREADS.value

        self._width = width
        self._height = height
        self._num_cells = width * height
        self._num_groups_x = int(ceil(float(width) / float(group_size_x)))
        self._num_groups_y = int(ceil(float(height) / float(group_size_y)))

    def _create_uniforms(self):
        self.size_uniform = bgfx.createUniform("_Size", bgfx.UniformType.Vec4)
        self.position_uniform = bgfx.createUniform("_Position", bgfx.UniformType.Vec4)
        self.radius_uniform = bgfx.createUniform("_Radius", bgfx.UniformType.Vec4)
        self.value_uniform = bgfx.createUniform("_Value", bgfx.UniformType.Vec4)
        self.static_uniform = bgfx.createUniform("_Static", bgfx.UniformType.Vec4)
        self.p1_uniform = bgfx.createUniform("_P1", bgfx.UniformType.Vec4)
        self.p2_uniform = bgfx.createUniform("_P2", bgfx.UniformType.Vec4)
        self.p3_uniform = bgfx.createUniform("_P3", bgfx.UniformType.Vec4)
        self.elapsed_time_uniform = bgfx.createUniform("_ElapsedTime", bgfx.UniformType.Vec4)
        self.speed_uniform = bgfx.createUniform("_Speed", bgfx.UniformType.Vec4)
        self.dissipation_uniform = bgfx.createUniform("_Dissipation", bgfx.UniformType.Vec4)
        self.velocity_uniform = bgfx.createUniform("_Velocity", bgfx.UniformType.Vec4)
        self.vorticity_scale_uniform = bgfx.createUniform("_VorticityScale", bgfx.UniformType.Vec4)
        self.alpha_uniform = bgfx.createUniform("_Alpha", bgfx.UniformType.Vec4)
        self.rbeta_uniform = bgfx.createUniform("_rBeta", bgfx.UniformType.Vec4)

    def _update_params(self, time_delta: float):
        bgfx.setUniform(self.elapsed_time_uniform,
                        as_void_ptr(np.array([time_delta, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.speed_uniform,
                        as_void_ptr(np.array([self.speed, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.dissipation_uniform,
                        as_void_ptr(np.array([self.dissipation, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.vorticity_scale_uniform,
                        as_void_ptr(np.array([self.vorticity, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))

        centre_factor = 1.0 / self.viscosity
        stencil_factor = 1.0 / (4.0 + centre_factor)

        bgfx.setUniform(self.alpha_uniform,
                        as_void_ptr(np.array([centre_factor, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))
        bgfx.setUniform(self.rbeta_uniform,
                        as_void_ptr(np.array([stencil_factor, 0.0, 0.0, 0.0]).astype(np.float32).tobytes()))

    def _init_compute_kernels(self):
        bgfx.setUniform(self.size_uniform,
                        as_void_ptr(np.array([self._width, self._height, 0.0, 0.0]).astype(np.float32).tobytes()))

        bgfx.setBuffer(TemplateConstants.VELOCITY_IN.value, self._velocity_buffer[self.VELOCITY_READ], bgfx.Access.Read)
        bgfx.setBuffer(TemplateConstants.VELOCITY_OUT.value, self._velocity_buffer[self.VELOCITY_WRITE],
                       bgfx.Access.Write)

        bgfx.setBuffer(TemplateConstants.PRESSURE_IN.value, self._pressure_buffer[self.PRESSURE_READ], bgfx.Access.Read)
        bgfx.setBuffer(TemplateConstants.PRESSURE_OUT.value, self._pressure_buffer[self.PRESSURE_WRITE],
                       bgfx.Access.Write)

        bgfx.setBuffer(TemplateConstants.DIVERGENCE.value, self._divergence_buffer, bgfx.Access.WRITE)
        bgfx.setBuffer(TemplateConstants.VORTICITY.value, self._vorticity_buffer, bgfx.Access.WRITE)
        bgfx.setBuffer(TemplateConstants.OBSTACLES.value, self._obstacles_buffer, bgfx.Access.WRITE)

    def _create_buffers(self):
        self._velocity_buffer = [
            create_vector2_buffer(self._num_cells, self.vertex_decl),
            create_vector2_buffer(self._num_cells, self.vertex_decl),
        ]
        self._pressure_buffer = [
            create_point_buffer(self._num_cells, self.vertex_decl),
            create_point_buffer(self._num_cells, self.vertex_decl),
        ]
        self._divergence_buffer = create_point_buffer(self._num_cells, self.vertex_decl)
        self._vorticity_buffer = create_point_buffer(self._num_cells, self.vertex_decl)
        self._obstacles_buffer = create_vector2_buffer(self._num_cells, self.vertex_decl)

    def _load_compute_kernels(self):
        self._add_velocity_kernel = bgfx.createProgram(load_shader("shader.AddVelocity.comp"), True)
        self._init_boundaries_kernel = bgfx.createProgram(load_shader("shader.InitBoundaries.comp"), True)
        self._advect_velocity_kernel = bgfx.createProgram(load_shader("shader.AdvectVelocity.comp"), True)
        self._divergence_kernel = bgfx.createProgram(load_shader("shader.Divergence.comp"), True)
        self._poisson_kernel = bgfx.createProgram(load_shader("shader.Poisson.comp"), True)
        self._subtract_gradient_kernel = bgfx.createProgram(load_shader("shader.SubtractGradient.comp"), True)
        self._calc_vorticity_kernel = bgfx.createProgram(load_shader("shader.CalcVorticity.comp"), True)
        self._apply_vorticity_kernel = bgfx.createProgram(load_shader("shader.ApplyVorticity.comp"), True)
        self._add_circle_obstacle_kernel = bgfx.createProgram(load_shader("shader.AddCircleObstacle.comp"), True)
        self._add_triangle_obstacle_kernel = bgfx.createProgram(load_shader("shader.AddTriangleObstacle.comp"), True)
        self._clear_buffer_kernel = bgfx.createProgram(load_shader("shader.ClearBuffer.comp"), True)
        self._viscosity_kernel = bgfx.createProgram(load_shader("shader.Viscosity.comp"), True)

    def _flip_velocity_buffer(self):
        tmp = self.VELOCITY_READ
        self.VELOCITY_READ = self.VELOCITY_WRITE
        self.VELOCITY_WRITE = tmp

        bgfx.setBuffer(TemplateConstants.VELOCITY_IN.value, self._velocity_buffer[self.VELOCITY_READ], bgfx.Access.Read)
        bgfx.setBuffer(TemplateConstants.VELOCITY_OUT.value, self._velocity_buffer[self.VELOCITY_WRITE],
                       bgfx.Access.Write)

    def _flip_pressure_buffer(self):
        tmp = self.PRESSURE_READ
        self.PRESSURE_READ = self.PRESSURE_WRITE
        self.PRESSURE_WRITE = tmp

        bgfx.setBuffer(TemplateConstants.PRESSURE_IN.value, self._pressure_buffer[self.PRESSURE_READ], bgfx.Access.Read)
        bgfx.setBuffer(TemplateConstants.PRESSURE_OUT.value, self._pressure_buffer[self.PRESSURE_WRITE],
                       bgfx.Access.Write)

    def __del__(self):
        # Destroy uniforms
        bgfx.destroy(self.size_uniform)
        bgfx.destroy(self.position_uniform)
        bgfx.destroy(self.radius_uniform)
        bgfx.destroy(self.value_uniform)
        bgfx.destroy(self.static_uniform)
        bgfx.destroy(self.p1_uniform)
        bgfx.destroy(self.p2_uniform)
        bgfx.destroy(self.p3_uniform)
        bgfx.destroy(self.elapsed_time_uniform)
        bgfx.destroy(self.speed_uniform)
        bgfx.destroy(self.dissipation_uniform)
        bgfx.destroy(self.vorticity_scale_uniform)
        bgfx.destroy(self.alpha_uniform)
        bgfx.destroy(self.rbeta_uniform)

        # Destroy buffers
        bgfx.destroy(self._velocity_buffer[0])
        bgfx.destroy(self._velocity_buffer[1])
        bgfx.destroy(self._pressure_buffer[0])
        bgfx.destroy(self._pressure_buffer[1])
        bgfx.destroy(self._divergence_buffer)
        bgfx.destroy(self._vorticity_buffer)
        bgfx.destroy(self._obstacles_buffer)

        # Destroy compute shaders
        bgfx.destroy(self._add_velocity_kernel)
        bgfx.destroy(self._init_boundaries_kernel)
        bgfx.destroy(self._advect_velocity_kernel)
        bgfx.destroy(self._divergence_kernel)
        bgfx.destroy(self._poisson_kernel)
        bgfx.destroy(self._subtract_gradient_kernel)
        bgfx.destroy(self._calc_vorticity_kernel)
        bgfx.destroy(self._apply_vorticity_kernel)
        bgfx.destroy(self._add_circle_obstacle_kernel)
        bgfx.destroy(self._add_triangle_obstacle_kernel)
        bgfx.destroy(self._clear_buffer_kernel)
        bgfx.destroy(self._viscosity_kernel)
