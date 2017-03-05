from __future__ import print_function

from dolfin import *
from matplotlib import pyplot, gridspec
import pytest

import os
import uuid
import gc

from fenapack import PCDKrylovSolver
from fenapack import PCDNewtonSolver
from fenapack import PCDProblem
from fenapack import StabilizationParameterSD


parameters["form_compiler"]["representation"] = "uflacs"
parameters["form_compiler"]["optimize"] = True
parameters["plotting_backend"] = "matplotlib"


@pytest.fixture
def data_dir():
    path = os.path.join(os.getcwd(), os.path.dirname(__file__),
                        os.pardir, os.pardir, "data")
    return os.path.realpath(path)


def get_random_string():
    return uuid.uuid4().hex


def create_function_space(refinement_level):
    # Load mesh from file and refine uniformly
    mesh = Mesh(os.path.join(data_dir(), "step_domain.xml.gz"))
    for i in range(refinement_level):
        mesh = refine(mesh)

    # Define and mark boundaries
    class Gamma0(SubDomain):
        def inside(self, x, on_boundary):
            return on_boundary
    class Gamma1(SubDomain):
        def inside(self, x, on_boundary):
            return on_boundary and near(x[0], -1.0)
    class Gamma2(SubDomain):
        def inside(self, x, on_boundary):
            return on_boundary and near(x[0], 5.0)
    boundary_markers = FacetFunction("size_t", mesh)
    boundary_markers.set_all(3)        # interior facets
    Gamma0().mark(boundary_markers, 0) # no-slip facets
    Gamma1().mark(boundary_markers, 1) # inlet facets
    Gamma2().mark(boundary_markers, 2) # outlet facets

    # Build Taylor-Hood function space
    P2 = VectorElement("Lagrange", mesh.ufl_cell(), 2)
    P1 = FiniteElement("Lagrange", mesh.ufl_cell(), 1)
    W = FunctionSpace(mesh, P2*P1)

    return W, boundary_markers


def create_forms(w, boundary_markers, nu, alpha, nls, ls):
    W = w.function_space()

    # Arguments and coefficients of the form
    u, p = TrialFunctions(W)
    v, q = TestFunctions(W)
    # FIXME: Which split is correct? Both work but one might use
    # restrict_as_ufc_function
    u_, p_ = split(w)
    #u_, p_ = w.split()

    if alpha != 1.0:
        alpha = Constant(alpha)
    else:
        # Optimize out the 1-alpha term
        alpha = float(alpha)

    # Nonlinear equation
    F = (
          Constant(nu)*inner(grad(u_), grad(v))
        + alpha*inner(dot(grad(u_), u_), v)
        + (1.0-alpha)*inner(div(outer(u_, u_)), v)
        - p_*div(v)
        - q*div(u_)
    )*dx

    # Jacobian
    if nls == "picard":
        J = (
              Constant(nu)*inner(grad(u), grad(v))
            + alpha*inner(dot(grad(u), u_), v)
            + (1.0-alpha)*inner(div(outer(u, u_)), v)
            - p*div(v)
            - q*div(u)
        )*dx
    elif nls == "newton":
        J = derivative(F, w)

    # No-slip BC
    bc0 = DirichletBC(W.sub(0), (0.0, 0.0), boundary_markers, 0)

    # Parabolic inflow BC
    inflow = Expression(("4.0*x[1]*(1.0 - x[1])", "0.0"), degree=2)
    bc1 = DirichletBC(W.sub(0), inflow, boundary_markers, 1)

    # Add stabilization for AMG 00-block
    if ls == "iterative":
        delta = StabilizationParameterSD(w.sub(0), Constant(nu))
        J_pc = J + delta*inner(dot(grad(u), u_), dot(grad(v), u_))*dx
    elif ls == "direct":
        J_pc = None

    return F, [bc0, bc1], J, J_pc


def create_pcd_problem(F, bcs, J, J_pc, w, nu, boundary_markers, pcd_variant):
    W = w.function_space()

    # Artificial BC for PCD preconditioner
    if pcd_variant == "BRM1":
        bc_pcd = DirichletBC(W.sub(1), 0.0, boundary_markers, 1)
    elif pcd_variant == "BRM2":
        bc_pcd = DirichletBC(W.sub(1), 0.0, boundary_markers, 2)

    # Arguments and coefficients of the form
    u, p = TrialFunctions(W)
    v, q = TestFunctions(W)
    # FIXME: Which split is correct? Both work but one might use
    # restrict_as_ufc_function
    u_, p_ = split(w)
    #u_, p_ = w.split()

    # PCD operators
    mp = Constant(1.0/nu)*p*q*dx
    kp = Constant(1.0/nu)*dot(grad(p), u_)*q*dx
    ap = inner(grad(p), grad(q))*dx
    if pcd_variant == "BRM2":
        n = FacetNormal(W.mesh())
        ds = Measure("ds", subdomain_data=boundary_markers)
        kp -= Constant(1.0/nu)*dot(u_, n)*p*q*ds(1)
        #kp -= Constant(1.0/nu)*dot(u_, n)*p*q*ds(0)  # TODO: Is this beneficial?

    # Collect forms to define nonlinear problem
    problem = PCDProblem(F, bcs, J, J_pc, ap=ap, kp=kp, mp=mp, bcs_pcd=bc_pcd)

    return problem


def create_solver(comm, pcd_variant, ls, mumps_debug=False):
    prefix = "s" + get_random_string() + "_"

    # Set up linear solver (GMRES with right preconditioning using Schur fact)
    linear_solver = PCDKrylovSolver(comm=comm)
    linear_solver.set_options_prefix(prefix)
    linear_solver.parameters["relative_tolerance"] = 1e-6
    #PETScOptions.set("ksp_monitor")
    PETScOptions.set(prefix+"ksp_gmres_restart", 150)

    # Set up subsolvers
    PETScOptions.set(prefix+"fieldsplit_p_pc_python_type", "fenapack.PCDPC_" + pcd_variant)
    if ls == "iterative":
        PETScOptions.set(prefix+"fieldsplit_u_ksp_type", "richardson")
        PETScOptions.set(prefix+"fieldsplit_u_ksp_max_it", 1)
        PETScOptions.set(prefix+"fieldsplit_u_pc_type", "hypre")
        PETScOptions.set(prefix+"fieldsplit_u_pc_hypre_type", "boomeramg")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Ap_ksp_type", "richardson")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Ap_ksp_max_it", 2)
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Ap_pc_type", "hypre")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Ap_pc_hypre_type", "boomeramg")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Mp_ksp_type", "chebyshev")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Mp_ksp_max_it", 5)
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Mp_ksp_chebyshev_eigenvalues", "0.5, 2.0")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Mp_pc_type", "jacobi")
    elif ls == "direct":
        # Debugging MUMPS
        if mumps_debug:
            PETScOptions.set(prefix+"fieldsplit_u_mat_mumps_icntl_4", 2)
            PETScOptions.set(prefix+"fieldsplit_p_PCD_Ap_mat_mumps_icntl_4", 2)
            PETScOptions.set(prefix+"fieldsplit_p_PCD_Mp_mat_mumps_icntl_4", 2)
    else:
        assert False

    # Apply options
    linear_solver.set_from_options()

    # Set up nonlinear solver
    solver = PCDNewtonSolver(linear_solver)
    solver.parameters["relative_tolerance"] = 1e-5

    return solver


@pytest.mark.parametrize("nu",          [0.02])
@pytest.mark.parametrize("alpha",       [1.0, 0.5])
@pytest.mark.parametrize("nls",         ["picard", "newton"])
@pytest.mark.parametrize("pcd_variant", ["BRM1", "BRM2"])
@pytest.mark.parametrize("ls",          ["direct", "iterative"])
def test_scaling_mesh(nu, alpha, nls, pcd_variant, ls, figure):
    set_log_level(WARNING)

    name = "nu_{}-alpha_{}-{}-{}-{}".format(nu, alpha, nls, pcd_variant, ls)
    results = {}

    # Iterate over refinement level
    #for level in range(7):
    for level in range(5):
        gc.collect()
        PETScOptions.clear()

        # Prepare problem and solvers
        with Timer("Prepare") as t_prepare:
            W, boundary_markers = create_function_space(level)
            w = Function(W)
            F, bcs, J, J_pc = create_forms(w, boundary_markers, nu, alpha, nls, ls)
            pcd_problem = create_pcd_problem(F, bcs, J, J_pc, w, nu,
                                             boundary_markers, pcd_variant)
            solver = create_solver(W.mesh().mpi_comm(), pcd_variant, ls,
                                   mumps_debug=False)
            prefix = solver.linear_solver().get_options_prefix()

            #PETScOptions.set(prefix+"ksp_monitor")
            #solver.linear_solver().set_from_options()

        # Solve
        with Timer("Solve") as t_solve:
            newton_iterations, converged = solver.solve(pcd_problem, w.vector())
        assert converged
        krylov_iterations = solver.krylov_iterations()

        # Postprocess data
        ndofs = W.dim()
        ndofs_u = W.sub(0).dim()
        ndofs_p = W.sub(1).dim()
        print(level, name, prefix, ndofs, ndofs_u, ndofs_p, krylov_iterations, t_solve.elapsed()[0])
        results[level] = {
            "ndofs": ndofs,
            "ndofs_u": ndofs_u,
            "ndofs_p": ndofs_p,
            "t_prepare": t_prepare.elapsed()[0],
            "t_solve": t_solve.elapsed()[0],
            "newton_iterations": newton_iterations,
            "krylov_iterations": krylov_iterations,
        }

    # Plot the case to common figure
    ndofs, num_iterations, t_solve = zip(*(
        (r["ndofs"], r["krylov_iterations"], r["t_solve"])
        for r in results.values()
    ))
    fig, (ax1, ax2) = figure
    ax1.plot(ndofs, num_iterations, '+--', label=name)
    ax2.plot(ndofs, t_solve, '+--', label=name)
    ax2.legend(bbox_to_anchor=(0, -0.05), loc=2, borderaxespad=0,
               fontsize='x-small', ncol=2)
    fig.savefig("scaling_mesh.pdf")


@pytest.fixture(scope='module')
def figure():
    fig = pyplot.figure()
    gs = gridspec.GridSpec(2, 1, height_ratios=[2, 2, 1], hspace=0.05)
    ax2 = fig.add_subplot(gs[1])
    ax1 = fig.add_subplot(gs[0], sharex=ax2)
    ax1.xaxis.set_label_position('top')
    ax1.xaxis.set_tick_params(labeltop='on', labelbottom='off')
    pyplot.setp(ax2.get_xticklabels(), visible=False)
    ax1.set_xscale('log')
    ax2.set_xscale('log')
    ax2.set_yscale('log')
    ax1.set_xlabel('Number dofs')
    ax1.set_ylabel('Number GMRES iterations')
    ax2.set_ylabel('CPU time')
    ax1.set_ylim(0, None, auto=True)
    ax2.set_ylim(0, None, auto=True)
    return fig, (ax1, ax2)
