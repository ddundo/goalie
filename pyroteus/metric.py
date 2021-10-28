"""
Driver functions for metric-based mesh adaptation.

.. rubric:: References

.. bibliography:: references.bib
    :filter: docname in docnames
"""
from __future__ import absolute_import
from .utility import *
from .interpolation import clement_interpolant
from . import kernel as kernels


__all__ = ["metric_complexity", "isotropic_metric", "anisotropic_metric", "hessian_metric",
           "enforce_element_constraints", "space_normalise", "space_time_normalise",
           "metric_relaxation", "metric_average", "metric_intersection", "combine_metrics",
           "determine_metric_complexity", "density_and_quotients", "check_spd"]


# --- General

@PETSc.Log.EventDecorator("pyroteus.metric_complexity")
def metric_complexity(metric, boundary=False):
    """
    Compute the complexity of a metric.

    This is the continuous analogue of the
    (discrete) mesh vertex count.

    :kwarg boundary: compute metric on domain
        interior or boundary?
    """
    differential = ufl.ds if boundary else ufl.dx
    return firedrake.assemble(ufl.sqrt(ufl.det(metric))*differential)


@PETSc.Log.EventDecorator("pyroteus.isotropic_metric")
def isotropic_metric(error_indicator, target_space=None, interpolant='Clement'):
    r"""
    Compute an isotropic metric from some error indicator.

    The result is a :math:`\mathbb P1` diagonal tensor
    field whose entries are projections of the error
    indicator in modulus.

    :arg error_indicator: the error indicator
    :kwarg target_space: :class:`TensorFunctionSpace` in
        which the metric will exist
    :kwarg interpolant: choose from 'Clement', 'L2',
        'interpolate', 'project'
    """
    mesh = error_indicator.ufl_domain()
    dim = mesh.topological_dimension()
    assert dim in (2, 3), f"Spatial dimension {dim:d} not supported."
    target_space = target_space or firedrake.TensorFunctionSpace(mesh, 'CG', 1)
    assert target_space.ufl_element().family() == 'Lagrange'
    assert target_space.ufl_element().degree() == 1

    # Interpolate P0 indicators into P1 space
    if interpolant == 'Clement':
        if not isinstance(error_indicator, firedrake.Function):
            raise NotImplementedError("Can only apply Clement interpolant to Functions")
        fs = error_indicator.function_space()
        family = fs.ufl_element().family()
        degree = fs.ufl_element().degree()
        if family != 'Lagrange' or degree != 1:
            if family != 'Discontinuous Lagrange' or degree != 0:
                raise ValueError("Was expecting an error indicator in P0 or P1"
                                 f" space, not {family} of degree {degree}")
            error_indicator = clement_interpolant(error_indicator)
    if interpolant in ('Clement', 'interpolate'):
        return firedrake.interpolate(abs(error_indicator)*ufl.Identity(dim), target_space)
    elif interpolant in ('L2', 'project'):
        error_indicator = firedrake.project(error_indicator, firedrake.FunctionSpace(mesh, "CG", 1))
        return firedrake.interpolate(abs(error_indicator)*ufl.Identity(dim), target_space)
    else:
        raise ValueError(f"Interpolant {interpolant} not recognised")


@PETSc.Log.EventDecorator("pyroteus.hessian_metric")
def hessian_metric(hessian):
    """
    Modify the eigenvalues of a Hessian matrix so
    that it is positive-definite.

    :arg hessian: the Hessian matrix
    """
    fs = hessian.function_space()
    shape = fs.ufl_element().value_shape()
    if len(shape) != 2:
        raise ValueError(
            "Expected a rank 2 tensor, "
            f"got rank {len(shape)}."
        )
    if shape[0] != shape[1]:
        raise ValueError(
            "Expected a square tensor field, "
            f"got {fs.ufl_element().value_shape()}."
        )
    metric = firedrake.Function(fs)
    kernel = kernels.eigen_kernel(kernels.metric_from_hessian, shape[0])
    op2.par_loop(kernel, fs.node_set, metric.dat(op2.RW), hessian.dat(op2.READ))
    return metric


def anisotropic_metric(error_indicator, hessian, **kwargs):
    r"""
    Compute an element-wise anisotropic metric from some
    error indicator, given a Hessian field.

    Two formulations are currently available:
      * the element-wise formulation presented
        in :cite:`CP13`; and
      * the vertex-wise formulation presented in
        :cite:`PP06`.

    In both cases, a :math:`\mathbb P1` metric is
    returned by default.

    :arg error_indicator: (list of) error indicator(s)
    :arg hessian: (list of) Hessian(s)
    :kwarg target_space: :class:`TensorFunctionSpace` in
        which the metric will exist
    :kwarg interpolant: choose from 'Clement', 'L2',
        'interpolate', 'project'
    """
    approach = kwargs.pop('approach', 'anisotropic_dwr')
    if approach == 'anisotropic_dwr':
        return anisotropic_dwr_metric(error_indicator, hessian, **kwargs)
    elif approach == 'weighted_hessian':
        return weighted_hessian_metric(error_indicator, hessian, **kwargs)
    else:
        raise ValueError(f"Anisotropic metric approach {approach} not recognised.")


@PETSc.Log.EventDecorator("pyroteus.anisotropic_dwr_metric")
def anisotropic_dwr_metric(error_indicator, hessian, target_space=None, interpolant='Clement', **kwargs):
    r"""
    Compute an anisotropic metric from some error
    indicator, given a Hessian field.

    The formulation used is based on that presented
    in :cite:`CP13`.

    Whilst an element-based formulation is used to
    derive the metric, the result is projected into
    :math:`\mathbb P1` space, by default.

    Note that normalisation is implicit in the metric
    construction and involves the `convergence_rate`
    parameter, named :math:`alpha` in :cite:`CP13`.

    :arg error_indicator: the error indicator
    :arg hessian: the Hessian
    :kwarg target_space: :class:`TensorFunctionSpace` in
        which the metric will exist
    :kwarg target_complexity: target metric complexity
    :kwarg convergence rate: normalisation parameter
    :kwarg interpolant: choose from 'Clement', 'L2',
        'interpolate', 'project'
    """
    if isinstance(error_indicator, list):  # FIXME: This is hacky
        assert len(error_indicator) == len(hessian)
        error_indicator = error_indicator[0]
        hessian = hessian[0]
    target_complexity = kwargs.get('target_complexity', None)
    assert target_complexity > 0.0, "Target complexity must be positive"
    mesh = hessian.ufl_domain()
    dim = mesh.topological_dimension()
    assert dim in (2, 3), f"Spatial dimension {dim:d} not supported."
    convergence_rate = kwargs.get('convergence_rate', 1.0)
    assert convergence_rate >= 1.0, "Convergence rate must be at least one"

    # Get reference element volume
    K_hat = 1/2 if dim == 2 else 1/6

    # Get current element volume
    P0 = firedrake.FunctionSpace(mesh, "DG", 0)
    K = firedrake.interpolate(K_hat*abs(ufl.JacobianDeterminant(mesh)), P0)

    # Get optimal element volume
    K_opt = firedrake.interpolate(pow(error_indicator, 1/(convergence_rate+1)), P0)
    K_opt.interpolate(K_opt.vector().gather().sum()/target_complexity*K/K_opt)

    # Interpolate from P1 to P0 by averaging the vertex-wise values
    P0_ten = firedrake.TensorFunctionSpace(mesh, "DG", 0)
    P0_metric = hessian_metric(firedrake.project(hessian, P0_ten))

    # Compute eigendecomposition
    P0_vec = firedrake.VectorFunctionSpace(mesh, "DG", 0)
    evectors, evalues = firedrake.Function(P0_ten), firedrake.Function(P0_vec)
    kernel = kernels.eigen_kernel(kernels.get_reordered_eigendecomposition, dim)
    op2.par_loop(
        kernel, P0_ten.node_set,
        evectors.dat(op2.RW), evalues.dat(op2.RW), P0_metric.dat(op2.READ)
    )

    # Compute stretching factors, in descending order
    if dim == 2:
        S = ufl.as_vector([
            ufl.sqrt(abs(evalues[0]/evalues[1])),
            ufl.sqrt(abs(evalues[1]/evalues[0])),
        ])
    else:
        S = ufl.as_vector([
            pow(abs((evalues[0]*evalues[0])/(evalues[1]*evalues[2])), 1/3),
            pow(abs((evalues[1]*evalues[1])/(evalues[2]*evalues[0])), 1/3),
            pow(abs((evalues[2]*evalues[2])/(evalues[0]*evalues[1])), 1/3),
        ])

    # Assemble metric
    evalues.interpolate(abs(K_hat/K_opt)*S)
    kernel = kernels.eigen_kernel(kernels.set_eigendecomposition, dim)
    op2.par_loop(
        kernel, P0_ten.node_set, P0_metric.dat(op2.RW),
        evectors.dat(op2.READ), evalues.dat(op2.READ)
    )

    # Interpolate the metric into target space and ensure SPD
    target_space = target_space or firedrake.TensorFunctionSpace(mesh, "CG", 1)
    if interpolant == 'Clement':
        return hessian_metric(clement_interpolant(P0_metric, target_space=target_space))
    elif interpolant == 'interpolate':
        return hessian_metric(firedrake.interpolate(P0_metric, target_space))
    elif interpolant in ('project', 'L2'):
        return hessian_metric(firedrake.project(P0_metric, target_space))
    else:
        raise ValueError(f"Interpolant {interpolant} not recognised")


@PETSc.Log.EventDecorator("pyroteus.weighted_hessian_metric")
def weighted_hessian_metric(error_indicators, hessians, target_space=None, interpolant='Clement', **kwargs):
    r"""
    Compute a vertex-wise anisotropic metric from a (list
    of) error indicator(s), given a (list of) Hessian
    field(s).

    The formulation used is based on that presented
    in :cite:`PP06`.

    :arg error_indicators: (list of) error indicator(s)
    :arg hessians: (list of) Hessian(s)
    :kwarg target_space: :class:`TensorFunctionSpace` in
        which the metric will exist
    :kwarg average: should metric components be averaged
        or intersected?
    :kwarg interpolant: choose from 'Clement', 'L2',
        'interpolate', 'project'
    """
    from collections.abc import Iterable
    if not isinstance(error_indicators, Iterable):
        error_indicators = [error_indicators]
    if not isinstance(hessians, Iterable):
        hessians = [hessians]
    mesh = hessians[0].ufl_domain()
    dim = mesh.topological_dimension()
    assert dim in (2, 3), f"Spatial dimension {dim:d} not supported."

    # Project error indicators into P1 space and use them to weight Hessians
    target_space = target_space or firedrake.TensorFunctionSpace(mesh, "CG", 1)
    metrics = [
        firedrake.Function(target_space, name="Metric")
        for hessian in hessians
    ]
    for error_indicator, hessian, metric in zip(error_indicators, hessians, metrics):
        if interpolant == 'Clement':
            error_indicator = clement_interpolant(error_indicator)
        else:
            P1 = firedrake.FunctionSpace(mesh, "CG", 1)
            if interpolant == 'interpolate':
                error_indicator = firedrake.interpolate(error_indicator, P1)
            elif interpolant in ('L2', 'project'):
                error_indicator = firedrake.project(error_indicator, P1)
            else:
                raise ValueError(f"Interpolant {interpolant} not recognised")
        metric.interpolate(abs(error_indicator)*hessian)

    # Combine the components
    return combine_metrics(*metrics, average=kwargs.get('average', False))


@PETSc.Log.EventDecorator("pyroteus.enforce_element_constraints")
def enforce_element_constraints(metrics, h_min, h_max, a_max, boundary_tag=None, optimise=False):
    """
    Post-process a list of metrics to enforce minimum and
    maximum element sizes, as well as maximum anisotropy.

    :arg metrics: the metrics
    :arg h_min: minimum tolerated element size,
        which could be a :class:`Function` or a number.
    :arg h_max: maximum tolerated element size,
        which could be a :class:`Function` or a number.
    :arg a_max: maximum tolerated element anisotropy,
        which could be a :class:`Function` or a number.
    :kwarg boundary_tag: optional tag to enforce sizes on.
    :kwarg optimise: is this a timed run?
    """
    from collections.abc import Iterable
    from firedrake import Function

    if isinstance(metrics, Function):
        metrics = [metrics]
    if not isinstance(h_min, Iterable):
        h_min = [h_min]*len(metrics)
    if not isinstance(h_max, Iterable):
        h_max = [h_max]*len(metrics)
    if not isinstance(a_max, Iterable):
        a_max = [a_max]*len(metrics)
    for metric, hmin, hmax, amax in zip(metrics, h_min, h_max, a_max):
        fs = metric.function_space()
        mesh = fs.mesh()

        # Interpolate hmin, hmax and amax into P1
        P1 = firedrake.FunctionSpace(mesh, "CG", 1)
        hmin = (clement_interpolant if isinstance(hmin, Function) else Function(P1).assign)(hmin)
        hmax = (clement_interpolant if isinstance(hmax, Function) else Function(P1).assign)(hmax)
        amax = (clement_interpolant if isinstance(amax, Function) else Function(P1).assign)(amax)

        # Check the values are okay
        if not optimise:
            _hmin = hmin.vector().gather().min()
            assert _hmin > 0.0
            assert hmax.vector().gather().min() > _hmin
            dx = ufl.dx(domain=mesh)
            assert np.isclose(firedrake.assemble(ufl.conditional(hmax < hmin, 1, 0)*dx), 0.0)
            assert amax.vector().gather().min() > 1.0

        # Enforce constraints
        dim = fs.mesh().topological_dimension()
        kernel = kernels.eigen_kernel(kernels.postproc_metric, dim)
        if boundary_tag is None:
            node_set = fs.node_set
        else:
            node_set = firedrake.DirichletBC(fs, 0, boundary_tag).node_set
        op2.par_loop(kernel, node_set,
                     metric.dat(op2.RW), hmin.dat(op2.READ),
                     hmax.dat(op2.READ), amax.dat(op2.READ))
    return metrics


# --- Normalisation

@PETSc.Log.EventDecorator("pyroteus.space_normalise")
def space_normalise(metric, target, p, global_factor=None, boundary=False):
    """
    Apply :math:`L^p` normalisation in space alone.

    :arg metric: :class:`Function` s corresponding to the
        metric to be normalised.
    :arg target: target metric complexity *in space alone*.
    :arg p: normalisation order.
    :kwarg global_factor: optional pre-computed global
        normalisation factor.
    :kwarg boundary: is the normalisation over the domain
        boundary?
    """
    assert p == 'inf' or p >= 1.0, f"Norm order {p} not valid"
    d = metric.function_space().mesh().topological_dimension()
    if boundary:
        d -= 1

    # Compute global normalisation factor
    if global_factor is None:
        detM = ufl.det(metric)
        dX = ufl.ds if boundary else ufl.dx
        integral = firedrake.assemble(pow(detM, 0.5 if p == 'inf' else p/(2*p + d))*dX)
        global_factor = firedrake.Constant(pow(target/integral, 2/d))

    # Normalise
    determinant = 1 if p == 'inf' else pow(ufl.det(metric), -1/(2*p + d))
    metric.interpolate(global_factor*determinant*metric)
    return metric


@PETSc.Log.EventDecorator("pyroteus.space_time_normalise")
def space_time_normalise(metrics, end_time, timesteps, target, p):
    """
    Apply :math:`L^p` normalisation in both space and time.

    :arg metrics: list of :class:`Function` s
        corresponding to the metric associated with
        each subinterval
    :arg end_time: end time of simulation
    :arg timesteps: list of timesteps specified in each
        subinterval
    :arg target: target *space-time* metric complexity
    :arg p: normalisation order
    """
    # NOTE: Assumes uniform subinterval lengths
    assert p == 'inf' or p >= 1.0, f"Norm order {p} not valid"
    num_subintervals = len(metrics)
    assert len(timesteps) == num_subintervals
    dt_per_mesh = [
        firedrake.Constant(end_time/num_subintervals/dt)
        for dt in timesteps
    ]
    d = metrics[0].function_space().mesh().topological_dimension()

    # Compute global normalisation factor
    integral = 0
    for metric, tau in zip(metrics, dt_per_mesh):
        detM = ufl.det(metric)
        if p == 'inf':
            integral += firedrake.assemble(tau*ufl.sqrt(detM)*ufl.dx)
        else:
            integral += firedrake.assemble(pow(tau**2*detM, p/(2*p + d))*ufl.dx)
    global_norm = firedrake.Constant(pow(target/integral, 2/d))

    # Normalise on each subinterval
    for metric, tau in zip(metrics, dt_per_mesh):
        determinant = 1 if p == 'inf' else pow(tau**2*ufl.det(metric), -1/(2*p + d))
        metric.interpolate(global_norm*determinant*metric)
    return metrics


@PETSc.Log.EventDecorator("pyroteus.determine_metric_complexity")
def determine_metric_complexity(H_interior, H_boundary, target, p, **kwargs):
    """
    Solve an algebraic problem to obtain coefficients
    for the interior and boundary metrics to obtain a
    given metric complexity.

    See :cite:`LD10` for details. Note that we use a
    slightly different formulation here.

    :arg H_interior: Hessian component from domain interior
    :arg H_boundary: Hessian component from domain boundary
    :arg target: target metric complexity
    :arg p: normalisation order
    :kwarg H_interior_scaling: optional scaling for interior component
    :kwarg H_boundary_scaling: optional scaling for boundary component
    """
    import sympy

    d = H_interior.function_space().mesh().topological_dimension()
    assert d in (2, 3)
    if p == 'inf':
        raise NotImplementedError  # TODO
    g = kwargs.get('H_interior_scaling', firedrake.Constant(1.0))
    gbar = kwargs.get('H_boundary_scaling', firedrake.Constant(1.0))
    g = pow(g, d/(2*p + d))
    gbar = pow(gbar, d/(2*p + d - 1))

    # Compute coefficients for the algebraic problem
    a = firedrake.assemble(g*pow(ufl.det(H_interior), p/(2*p + d))*ufl.dx)
    b = firedrake.assemble(gbar*pow(ufl.det(H_boundary), p/(2*p + d - 1))*ufl.ds)

    # Solve algebraic problem
    c = sympy.Symbol('c')
    c = sympy.solve(a*pow(c, d/2) + b*pow(c, (d-1)/2) - target, c)
    eq = f"{a}*c^{d/2} + {b}*c^{(d-1)/2} = {target}"
    if len(c) == 0:
        raise ValueError(f"No solutions found for equation {eq}.")
    elif len(c) > 1:
        raise ValueError(f"A unique solution could not be found for equation {eq}.")
    else:
        return float(c[0])


# --- Combination

@PETSc.Log.EventDecorator("pyroteus.metric_relaxation")
def metric_relaxation(*metrics, weights=None, function_space=None):
    """
    Combine a list of metrics with a weighted average.

    :arg metrics: the metrics to be combined
    :kwarg weights: a list of weights
    :kwarg function_space: the :class:`FunctionSpace`
        the relaxed metric should live in
    """
    n = len(metrics)
    assert n > 0, "Nothing to combine"
    weights = weights or np.ones(n)/n
    if len(weights) != n:
        raise ValueError(
            "Number of weights do not match number of metrics"
            + f"({len(weights)} vs. {n})")
    fs = function_space or metrics[0].function_space()
    relaxed_metric = firedrake.Function(fs)
    for weight, metric in zip(weights, metrics):
        if not isinstance(metric, firedrake.Function) or metric.function_space() != fs:
            metric = firedrake.interpolate(metric, function_space)
        relaxed_metric += weight*metric
    return relaxed_metric


def metric_average(*metrics, function_space=None):
    """
    Combine a list of metrics by averaging.

    :arg metrics: the metrics to be combined
    :kwarg function_space: the :class:`FunctionSpace`
        the averaged metric should live in
    """
    return metric_relaxation(*metrics, function_space=function_space)


@PETSc.Log.EventDecorator("pyroteus.metric_intersection")
def metric_intersection(*metrics, function_space=None, boundary_tag=None):
    """
    Combine a list of metrics by intersection.

    :arg metrics: the metrics to be combined
    :kwarg function_space: the :class:`FunctionSpace`
        the intersected metric should live in
    :kwarg boundary_tag: optional boundary segment physical
        ID for boundary intersection. Otherwise, the
        intersection is over the whole domain
    """
    n = len(metrics)
    assert n > 0, "Nothing to combine"
    fs = function_space or metrics[0].function_space()
    for metric in metrics:
        if not isinstance(metric, firedrake.Function) or metric.function_space() != fs:
            metric = firedrake.interpolate(metric, function_space)
    intersected_metric = firedrake.Function(metrics[0])
    if boundary_tag is None:
        node_set = fs.node_set
    elif boundary_tag == []:
        raise ValueError("It is unclear what to do with an empty list of boundary tags.")
    else:
        node_set = firedrake.DirichletBC(fs, 0, boundary_tag).node_set  # TODO: is there a cleaner way?
    dim = fs.mesh().topological_dimension()
    assert dim in (2, 3), f"Spatial dimension {dim:d} not supported."

    def intersect_pair(M1, M2):
        M12 = firedrake.Function(M1)
        kernel = kernels.eigen_kernel(kernels.intersect, dim)
        op2.par_loop(kernel, node_set, M12.dat(op2.RW), M1.dat(op2.READ), M2.dat(op2.READ))
        return M12

    for metric in metrics[1:]:
        intersected_metric = intersect_pair(intersected_metric, metric)
    return intersected_metric


def combine_metrics(*metrics, average=True, **kwargs):
    """
    Combine a list of metrics.

    :kwarg average: combination by averaging or intersection?
    """
    if average:
        if 'boundary_tag' in kwargs:
            kwargs.pop('boundary_tag')
        return metric_relaxation(*metrics, **kwargs)
    else:
        if 'weights' in kwargs:
            kwargs.pop('weights')
        return metric_intersection(*metrics, **kwargs)


# --- Metric decompositions and properties

@PETSc.Log.EventDecorator("pyroteus.density_and_quotients")
def density_and_quotients(metric, reorder=False):
    r"""
    Extract the density and anisotropy quotients from a
    metric.

    By symmetry, Riemannian metrics admit an orthogonal
    eigendecomposition,

    .. math::
        \underline{\mathbf M}(\mathbf x)
        = \underline{\mathbf V}(\mathbf x)\:
        \underline{\boldsymbol\Lambda}(\mathbf x)\:
        \underline{\mathbf V}(\mathbf x)^T,

    at each point :math:`\mathbf x\in\Omega`, where
    :math:`\underline{\mathbf V}` and
    :math:`\underline{\boldsymbol\Sigma}` are matrices
    holding the eigenvectors and eigenvalues, respectively.
    By positive-definiteness, entries of
    :math:`\underline{\boldsymbol\Lambda}` are all positive.

    An alternative decomposition,

    .. math::
        \underline{\mathbf M}(\mathbf x)
        = d(\mathbf x)^\frac2n
        \underline{\mathbf V}(\mathbf x)\:
        \underline{\mathbf R}(\mathbf x)^{-\frac2n}
        \underline{\mathbf V}(\mathbf x)^T

    can also be deduced, in terms of the <em>metric density</em>
    and <em>anisotropy quotients</em>,

    .. math::
        d = \sum_{i=1}^n h_i,\qquad
        r_i = \frac{h_i^n}d,\qquad \forall i=1:n,

    where :math:`h_i := \frac1{\sqrt{\lambda_i}}`.

    :arg metric: the metric to extract density and
        quotients from
    :kwarg reorder: should the eigendecomposition be
        reordered?
    """
    fs_ten = metric.function_space()
    mesh = fs_ten.mesh()
    fe = (fs_ten.ufl_element().family(), fs_ten.ufl_element().degree())
    fs_vec = firedrake.VectorFunctionSpace(mesh, *fe)
    fs = firedrake.FunctionSpace(mesh, *fe)
    dim = mesh.topological_dimension()

    # Setup fields
    evectors = firedrake.Function(fs_ten)
    evalues = firedrake.Function(fs_vec)
    density = firedrake.Function(fs, name="Metric density")
    quotients = firedrake.Function(fs_vec, name="Anisotropic quotients")

    # Compute eigendecomposition
    if reorder:
        kernel = kernels.eigen_kernel(kernels.get_reordered_eigendecomposition, dim)
    else:
        kernel = kernels.eigen_kernel(kernels.get_eigendecomposition, dim)
    op2.par_loop(kernel, fs_ten.node_set,
                 evectors.dat(op2.RW), evalues.dat(op2.RW), metric.dat(op2.READ))

    # Extract density and quotients
    h = [1/sqrt(evalues[i]) for i in range(dim)]
    density.interpolate(1/prod(h))
    quotients.interpolate(as_vector([density*h[i]**dim for i in range(dim)]))
    return density, quotients


@PETSc.Log.EventDecorator("pyroteus.is_symmetric")
def is_symmetric(M, rtol=1.0e-08):
    """
    Determine whether a tensor field is symmetric.

    :arg M: the tensor field
    :kwarg rtol: relative tolerance for the test
    """
    err = firedrake.assemble(abs(ufl.det(M - ufl.transpose(M)))*ufl.dx)
    return err/firedrake.assemble(abs(ufl.det(M))*ufl.dx) < rtol


@PETSc.Log.EventDecorator("pyroteus.is_pos_def")
def is_pos_def(M):
    """
    Determine whether a tensor field is positive-definite.

    :arg M: the tensor field
    """
    fs = M.function_space()
    element = (fs.ufl_element().family(), fs.ufl_element().degree())
    fs_vec = firedrake.VectorFunctionSpace(fs.mesh(), *element)
    evectors = firedrake.Function(fs)
    evalues = firedrake.Function(fs_vec)
    kernel = kernels.eigen_kernel(kernels.get_eigendecomposition, fs.mesh().topological_dimension())
    op2.par_loop(kernel, fs.node_set, evectors.dat(op2.RW), evalues.dat(op2.RW), M.dat(op2.READ))
    return evalues.vector().gather().min() > 0.0


def is_spd(M):
    """
    Determine whether a tensor field is symmetric positive-definite.

    :arg M: the tensor field
    """
    return is_symmetric(M) and is_pos_def(M)


def check_spd(M):
    """
    Verify that a tensor field is symmetric positive-definite.

    :arg M: the tensor field
    """
    try:
        assert is_symmetric(M)
    except AssertionError:
        raise ValueError("FAIL: Matrix is not symmetric")
    try:
        assert is_pos_def(M)
    except AssertionError:
        raise ValueError("FAIL: Matrix is not positive-definite")
