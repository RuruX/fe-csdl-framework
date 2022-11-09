"""
Structural analysis for the undeflected common research model (uCRM)
uCRM-9 Specifications: (units: m/ft, kg/lb)
(from https://deepblue.lib.umich.edu/bitstream/handle/2027.42/143039/6.2017-4456.pdf?sequence=1)
Maximum take-off weight	352,400kg/777,000lb
Wing span (extended)    71.75m/235.42ft
Overall length	        76.73m/251.75ft
"""

from fe_csdl_opt.fea.fea_dolfinx import *
from fe_csdl_opt.csdl_opt.fea_model import FEAModel
from fe_csdl_opt.csdl_opt.state_model import StateModel
from fe_csdl_opt.csdl_opt.output_model import OutputModel
from fe_csdl_opt.csdl_opt.pre_processor.general_filter_model \
                                    import GeneralFilterModel
import numpy as np
import csdl
from csdl import Model
from csdl_om import Simulator as om_simulator
from python_csdl_backend import Simulator as py_simulator
from matplotlib import pyplot as plt
import argparse
from mpi4py import MPI
from shell_analysis_fenicsx import *
from shell_analysis_fenicsx.read_properties import readCLT, sortIndex

from FSI_coupling.VLM_sim_handling import *
from FSI_coupling.shellmodule_utils import *
from FSI_coupling.NodalMapping import *

# from dolfinx.io import XDMFFile
# from dolfinx.fem.petsc import (assemble_vector, assemble_matrix, apply_lifting)
# from dolfinx.fem import (locate_dofs_topological, locate_dofs_geometrical,
#                         dirichletbc, form, Constant, VectorFunctionSpace, assemble)
# from dolfinx.mesh import locate_entities

from scipy.sparse import csr_array, vstack
from scipy.sparse.linalg import spsolve
from scipy.sparse.linalg import inv as sp_inv

##########################################################################
######################## Structural inputs ###############################
##########################################################################
tri_mesh = [
            "eVTOL_wing_half_tri_77020_103680.xdmf", # error
            "eVTOL_wing_half_tri_81475_109456.xdmf", # error
            "eVTOL_wing_half_tri_107695_136686.xdmf",
            "eVTOL_wing_half_tri_135957_170304.xdmf"] # error

quad_mesh = [
            "eVTOL_wing_half_quad_77020_51840.xdmf", # error
            "eVTOL_wing_half_quad_81475_54228.xdmf", # error
            "eVTOL_wing_half_quad_107695_68343.xdmf", # error
            "eVTOL_wing_half_quad_135957_85152.xdmf",] # error
test = 2
# s_mesh_file_name = quad_mesh[test]
s_mesh_file_name = tri_mesh[test]
path = "evtol_wing/"
solid_mesh_file = path + s_mesh_file_name

with XDMFFile(MPI.COMM_WORLD, solid_mesh_file, "r") as xdmf:
       mesh = xdmf.read_mesh(name="Grid")
nel = mesh.topology.index_map(mesh.topology.dim).size_local
nn = mesh.topology.index_map(0).size_local

# define structural properties
E = 6.8E10 # unit: Pa (N/m^2)
nu = 0.35
h = 3E-3 # overall thickness (unit: m)
rho_struct = 2710. # unit: kg/m^3

element_type = "CG2CG1" # with quad/tri elements

element = ShellElement(
                mesh,
                element_type,
#                inplane_deg=3,
#                shear_deg=3
                )
dx_inplane, dx_shear = element.dx_inplane, element.dx_shear



def pdeRes(h,w,E,f,CLT,dx_inplane,dx_shear,rho,uddot,thetaddot):
    w_temp = Function(w.function_space)
    dw = TestFunction(w.function_space)
    elastic_model = DynamicElasticModel(mesh, w_temp, CLT)
    elastic_energy = elastic_model.elasticEnergy(E, h, dx_inplane, dx_shear)
    ALPHA = 1
    dWint = elastic_model.weakFormResidual(ALPHA, elastic_energy, w_temp, dw, f)
    dWint_mid = ufl.replace(dWint, {w_temp: w_mid})
    # Inertial contribution to the residual:
    dWmass = elastic_model.inertialResidual(rho,h,uddot,thetaddot)
    F = dWmass + dWint_mid
    return F

def compliance(u_mid,h):
    h_mesh = CellDiameter(mesh)
    alpha = 1e-1
    dX = ufl.Measure('dx', domain=mesh, metadata={"quadrature_degree":0})
    return 0.5*dot(u_mid,u_mid)*dX \
            + 0.5*alpha*dot(grad(h), grad(h))*(h_mesh**2)*dX

def volume(h):
    return h*dx

###########################################################################
######################## Aerodynamic inputs ###############################
###########################################################################
f_mesh_file_name = 'evtol_wing_vlm_mesh.npy'

vlm_mesh_file = path+ f_mesh_file_name
# define VLM input parameters



V_inf = 50.  # freestream velocity magnitude in m/s
V_p = 50. # peak velocity of the gust
# Time-stepping parameters
# T       = 3.
l_chord = 1.2 # unit: m, chord length
GGLc = 5 # gust gradient length in chords
T0 = 0.02 # static before the gust
T1 = GGLc*l_chord/V_inf
T2 = 0.02 # calm down
T = T0+T1+T2
Nsteps  = 20
dt = T/Nsteps
def V_g(t):
    V_g = 0.
    if T0 <= t <= T0+T1:
        # V_g = V_p*np.sin(2*np.pi*t/T1)**2
        V_g = V_p*(1-np.cos(2*np.pi*(t-T0)/T1))
    return V_g
    # return 1/2*V_p*(1-np.cos(2*np.pi*t/GGLc))

# AoA = 6.  # Angle of Attack in degrees
AoA = 0.  # Angle of Attack in degrees
AoA_rad = np.deg2rad(AoA)  # Angle of Attack converted to radians
rho = 1.225  # International Standard Atmosphere air density at sea level in kg/m^3

conv_eps = 1e-6  # Convergence tolerance for iterative solution approach
iterating = True

################### Import Aerodynamic mesh ###################
print("Constructing aerodynamic mesh and mesh mappings...")
# Import a preconstructed VLM mesh
VLM_mesh = load_VLM_mesh(vlm_mesh_file, np.array([4.28, 0., 2.96]))
VLM_mesh_coordlist_baseline = reshape_3D_array_to_2D(VLM_mesh)

############### Define force functions and aero-elastic coupling object #################
coupling_obj = FEniCSx_VLM_coupling(mesh, VLM_mesh)

f_dist_solid = Function(coupling_obj.solid_aero_force_space)
f_nodal_solid = Function(coupling_obj.solid_aero_force_space)

#######################################################
############## The optimization problem ###############
#######################################################
fea = FEA(mesh)
# Add input to the PDE problem:
input_name = 'thickness'
input_function_space = FunctionSpace(mesh, ("DG", 0))
input_function = Function(input_function_space)

# Add state to the PDE problem:
state_name = 'displacements'
state_function_space = element.W
state_function = Function(state_function_space)
##################################
u, theta = split(state_function)
dw = TestFunction(state_function_space)
du_mid,dtheta = split(dw)

# Quantities from the previous time step
w_old = Function(state_function_space)
u_old, theta_old = split(w_old)
wdot_old = Function(state_function_space)
udot_old, thetadot_old = split(wdot_old)

# Set up the time integration scheme
def implicitMidpointRule(u, u_old, udot_old, dt):
    u_mid = Constant(mesh, 0.5)*(u_old+u)
    udot = Constant(mesh, 2/dt)*u - Constant(mesh, 2/dt)*u_old - udot_old
    uddot = (udot - udot_old)/dt
    return u_mid, udot, uddot
def wdot_vector(w, w_old, wdot_old, dt):
    return 2/dt*w.vector - 2/dt*w_old.vector - wdot_old.vector

u_mid, udot, uddot = implicitMidpointRule(u, u_old, udot_old, dt)
theta_mid, thetadot, thetaddot = implicitMidpointRule(theta, theta_old, thetadot_old, dt)
w_mid = Constant(mesh, 0.5)*(w_old+state_function)
##################################
material_model = MaterialModel(E=E,nu=nu,h=input_function) # Simple isotropic material
residual_form = pdeRes(input_function,state_function,
                        E,f_dist_solid,material_model.CLT,dx_inplane,dx_shear,
                        rho_struct, uddot, thetaddot)

# Add output to the PDE problem:
output_name_1 = 'compliance'
output_form_1 = compliance(state_function.sub(0), input_function)
output_name_2 = 'volume'
output_form_2 = volume(input_function)


fea.add_input(input_name, input_function)
fea.add_state(name=state_name,
                function=state_function,
                residual_form=residual_form,
                arguments=[input_name])
fea.add_output(name=output_name_1,
                type='scalar',
                form=output_form_1,
                arguments=[state_name,input_name])
fea.add_output(name=output_name_2,
                type='scalar',
                form=output_form_2,
                arguments=[input_name])

############ Set the BCs for the airplane model ###################

locate_BC1 = locate_dofs_geometrical((state_function_space.sub(0), state_function_space.sub(0).collapse()[0]),
                                    lambda x: np.less(x[1], 0.55))
locate_BC2 = locate_dofs_geometrical((state_function_space.sub(1), state_function_space.sub(1).collapse()[0]),
                                    lambda x: np.less(x[1], 0.55))
ubc = Function(state_function_space)
with ubc.vector.localForm() as uloc:
     uloc.set(0.)
############ Strongly enforced boundary conditions #############
fea.add_strong_bc(ubc, [locate_BC1], state_function_space.sub(0))
fea.add_strong_bc(ubc, [locate_BC2], state_function_space.sub(1))

PATH = "records/"
xdmf_file = XDMFFile(comm, PATH+"u_mid.xdmf", "w")
xdmf_file.write_mesh(mesh)
xdmf_file_aero_f = XDMFFile(comm, PATH+"aero_F.xdmf", "w")
xdmf_file_aero_f.write_mesh(mesh)
xdmf_file_aero_f_nodal = XDMFFile(comm, PATH+"aero_F_nodal.xdmf", "w")
xdmf_file_aero_f_nodal.write_mesh(mesh)
state_function.sub(0).name = 'u_mid'
state_function.sub(1).name = 'theta'
################# Dynamic aerostructural coupling solve ####################
def solveDynamicAeroelasticity(res,func,bc,report=False):

    t = 0.0

    for i in range(0, Nsteps):
        t += dt
        print("------- Time step "+str(i+1)+"/"+str(Nsteps)
                +" , t = "+str(t)+" -------")

        # Solve the nonlinear problem for this time step and put the solution
        # (in homogeneous coordinates) in y_hom.
        solveAeroelasticity(res,func,bc,t,report=report)

        # Advance to the next time step
        # ** since u_dot, theta_dot are not functions, we cannot directly
        # ** interpolate them onto wdot_old.
        wdot_old.vector[:] = wdot_vector(func,w_old,wdot_old, dt)
        w_old.interpolate(func)

        # Save solution to XDMF format
        xdmf_file.write_function(func.sub(0), t)
        xdmf_file_aero_f.write_function(f_dist_solid, t)
        xdmf_file_aero_f_nodal.write_function(f_nodal_solid, t)

################# Static aerostructural coupling solve ####################
def solveAeroelasticity(res,func,bc,t,report=False):

    ########## Start of iteration loop for coupled solution procedure ##########
    print("Start of iteration loop...")
    # declare initial values for variables that will be updated during each iteration
    VLM_mesh_displaced = deepcopy(VLM_mesh_coordlist_baseline)
    func_old = np.zeros_like(func.vector.getArray())
    iterating = True
    it_count = 0
    while iterating:
        print("Running VLM sim...")
        ########## Update VLM mesh with deformation and run VLM sim: ##########
        VLM_mesh_transposed = construct_VLM_transposed_input_mesh(VLM_mesh_displaced, VLM_mesh.shape)

        VLM_sim = VLM_CADDEE([VLM_mesh_transposed], AoA, np.array([V_inf*np.cos(AoA_rad), 0., V_inf*np.sin(AoA_rad)+V_g(t)]), rho=rho)

        # extract panel forces from VLM simulation
        panel_forces = VLM_sim.sim['panel_forces'][0]
        panel_forces = panel_forces*np.array([-1, 1, 1])  # multiply x-component with -1 to account for difference in reference frames

        print("Total aero force: {}".format(list(np.sum(panel_forces, axis=0))))

        ########## Project VLM panel forces to solid CG1 space: ##########

        # compute and set distributed vlm load
        F_dist_solid = coupling_obj.compute_dist_solid_force_from_vlm(panel_forces)
        f_dist_solid.vector.setArray(F_dist_solid)

        print("Total aero force projected to solid: {}".format([assemble_scalar(form(f_dist_solid[i]*dx)) for i in range(3)]))

        # ########## Update residual of the weak form with new aero force: ##########
        # F = elastic_model.weakFormResidual(elastic_energy, f_dist_solid)

        ########## Solve with Newton solver wrapper: ##########
        print("Running solid shell sim...")
        solveNonlinear(res,func,bc)

        ########## Update displacements: ##########
        print("Updating displacements...")
        # compute VLM mesh displacement from the solid solution vector
        vlm_disp_vec = coupling_obj.compute_vlm_displacement_from_solid(func)

        # add current displacement to baseline VLM mesh
        VLM_mesh_displaced = np.add(VLM_mesh_coordlist_baseline, vlm_disp_vec)

        it_count += 1
        func_new = func.vector.getArray()
        _, _, uZ = computeNodalDisp(func.sub(0))
        print("Iteration: {}".format(it_count))
        print("Tip deflection:", max(uZ))
        print("Max difference w.r.t. previous iteration: {}".format(np.max(np.abs(np.subtract(func_old, func_new)))))

        if np.max(np.abs(np.subtract(func_old, func_new))) <= conv_eps:
            iterating = False
            print("Convergence criterion met...")

            # map VLM panel forces to solid nodal forces
            F_nodal_solid = coupling_obj.compute_nodal_solid_force_from_vlm(panel_forces)
            # set nodal pressure load (just for plotting purposes)
            f_nodal_solid.vector.setArray(F_nodal_solid)

        func_old = deepcopy(func_new)

fea.custom_solve = solveDynamicAeroelasticity


'''
4. Set up the CSDL model
'''


fea.PDE_SOLVER = 'Newton'
# fea.REPORT = True
fea_model = FEAModel(fea=[fea])
fea_model.create_input("{}".format(input_name),
                            shape=fea.inputs_dict[input_name]['shape'],
                            val=h*np.ones(fea.inputs_dict[input_name]['shape']))

# fea_model.add_design_variable(input_name)
# fea_model.add_objective(output_name)

sim = py_simulator(fea_model)
# sim = om_simulator(fea_model)

########### Test the forward solve ##############

sim.run()
########## Output: ##############
dofs = len(state_function.vector.getArray())
uZ = computeNodalDisp(state_function.sub(0))[2]
print("-"*50)
print("-"*8, s_mesh_file_name, "-"*9)
print("-"*50)
print("Tip deflection:", max(uZ))
print("  Number of elements = "+str(nel))
print("  Number of vertices = "+str(nn))
print("  Number of total dofs = ", dofs)
print("-"*50)

########## Visualization: ##############
u_mid, _ = state_function.split()

with XDMFFile(MPI.COMM_WORLD, "solutions/u_mid_tri_"+str(dofs)+".xdmf", "w") as xdmf:
    xdmf.write_mesh(mesh)
    xdmf.write_function(u_mid)

with XDMFFile(MPI.COMM_WORLD, "solutions/aero_F_"+str(dofs)+".xdmf", "w") as xdmf:
    xdmf.write_mesh(mesh)
    xdmf.write_function(f_dist_solid)

with XDMFFile(MPI.COMM_WORLD, "solutions/aero_F_nodal_"+str(dofs)+".xdmf", "w") as xdmf:
    xdmf.write_mesh(mesh)
    xdmf.write_function(f_nodal_solid)
