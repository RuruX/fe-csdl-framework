"""
Aerostructural analysis on an eVTOL wing model
"""

from femo.fea.fea_dolfinx import *
from femo.csdl_opt.fea_model import FEAModel
from femo.csdl_opt.state_model import StateModel
from femo.csdl_opt.output_model import OutputModel
import numpy as np
import csdl
from csdl import Model
from python_csdl_backend import Simulator
from matplotlib import pyplot as plt
import argparse
from mpi4py import MPI
from shell_analysis_fenicsx import *
from shell_analysis_fenicsx.read_properties import readCLT, sortIndex

from FSI_coupling.VLM_sim_handling import *
from FSI_coupling.shellmodule_utils import *
from FSI_coupling.NodalMapping import *
from FSI_coupling.NodalMapping import *
from FSI_coupling.mesh_handling_utils import *
from FSI_coupling.array_handling_utils import *
from FSI_coupling.shellmodule_csdl_interface import (
                                DisplacementMappingImplicitModel, 
                                ForceMappingModel, 
                                VLMForceIOModel, 
                                VLMMeshUpdateModel
                                )
import cProfile, pstats, io
from mpi4py import MPI

def profile(filename=None, comm=MPI.COMM_WORLD):
    def prof_decorator(f):
        def wrap_f(*args, **kwargs):
            pr = cProfile.Profile()
            pr.enable()
            result = f(*args, **kwargs)
            pr.disable()

            if filename is None:
                pr.print_stats()
            else:
                filename_r = filename + ".{}".format(comm.Get_rank())
                pr.dump_stats(filename_r)

            return result
        return wrap_f
    return prof_decorator
##########################################################################
######################## Structural inputs ###############################
##########################################################################

s_mesh_file_name = "eVTOL_wing_half_tri_107695_136686.xdmf"
f_mesh_file_name = 'vlm_mesh_nx2_ny10.npy'
path = "../../evtol_wing_mesh/"
solid_mesh_file = path + s_mesh_file_name
vlm_mesh_file = path+ f_mesh_file_name

with XDMFFile(MPI.COMM_WORLD, solid_mesh_file, "r") as xdmf:
       solid_mesh = xdmf.read_mesh(name="Grid")
nel = solid_mesh.topology.index_map(solid_mesh.topology.dim).size_local
nn = solid_mesh.topology.index_map(0).size_local

# define structural properties
E = 6.8E10 # unit: Pa (N/m^2)
nu = 0.35
h_val = 3E-3 # overall thickness (unit: m)
y_bc = 0.6
PENALTY_BC = True

element_type = "CG2CG1" # with quad/tri elements

element = ShellElement(solid_mesh,element_type,)
dx_inplane, dx_shear = element.dx_inplane, element.dx_shear


def pdeRes(h,w,E,f,CLT,dx_inplane,dx_shear,penalty=False, dss=ds, dSS=dS, g=None):
    elastic_model = ElasticModel(solid_mesh,w,CLT)
    elastic_energy = elastic_model.elasticEnergy(E, h, dx_inplane,dx_shear)
    return elastic_model.weakFormResidual(elastic_energy, f, 
                                        penalty=penalty, dss=dss, dSS=dSS, g=g)

def regularization(h, type=None):
    alpha1 = Constant(solid_mesh, 1e3)
    alpha2 = Constant(solid_mesh, 1e0)
    h_mesh = CellDiameter(solid_mesh)

    regularization = 0.
    if type=='H1':
        # H1 regularization
        regularization = 0.5*alpha1*dot(grad(h),grad(h))*dx
    elif type=='L2H1':
        # L2 + H1 regularization
        regularization = 0.5*alpha1*inner(h,h)*dx + \
                           0.5*alpha2*h_mesh**2*inner(grad(h),grad(h))*dx
    elif type=='L2':
        # L2 regularization
        regularization = 0.5*alpha1*inner(h,h)*dx
    # No regularization
    return regularization

def compliance(u_mid,h,dxx):
    return Constant(solid_mesh, 0.5)*inner(u_mid,u_mid)*dxx + regularization(h)

def new_compliance(u_mid,f,dxx):
    return inner(u_mid,f)*dxx

def volume(h):
    return h*dx

def elastic_energy(w,CLT,E,h,dx_inplane,dx_shear):
    elastic_model = ElasticModel(solid_mesh,w,CLT)
    elastic_energy = elastic_model.elasticEnergy(E, h, dx_inplane,dx_shear)
    return elastic_energy


#### Getting facets of the LEFT and the RIGHT edge  ####



#### Getting facets of the LEFT and the RIGHT edge  ####
DOLFIN_EPS = 3E-16
def ClampedBoundary(x):
    return np.less_equal(x[1], y_bc)
def rightChar(x):
    return np.greater(x[1], 5.2) # measure deflection near wing tip
fdim = solid_mesh.topology.dim - 1
facets_1 = locate_entities_boundary(solid_mesh,fdim,ClampedBoundary)
facets_11 = locate_entities(solid_mesh,fdim,ClampedBoundary)
facets_2 = locate_entities_boundary(solid_mesh,fdim,rightChar)
#### Defining measures ####
facet_tag_1 = meshtags(solid_mesh, fdim, facets_1,
                    np.full(len(facets_1),100,dtype=np.int32))
facet_tag_11 = meshtags(solid_mesh, fdim, facets_11,
                    np.full(len(facets_11),100,dtype=np.int32))
metadata = {"quadrature_degree":4}
ds_1 = ufl.Measure('ds',domain=solid_mesh,subdomain_data=facet_tag_1,metadata=metadata)
dS_1 = ufl.Measure('dS',domain=solid_mesh,subdomain_data=facet_tag_11,metadata=metadata)

area_2 = dolfinx.mesh.locate_entities(solid_mesh,fdim+1,rightChar)
area_tag_2 = meshtags(solid_mesh, fdim+1, area_2,
                    np.full(len(area_2),10,dtype=np.int32))
dx_2 = ufl.Measure('dx',domain=solid_mesh,subdomain_data=area_tag_2,metadata=metadata)


###########################################################################
######################## Aerodynamic inputs ###############################
###########################################################################

# define vlm input parameters
V_inf = 50.  # freestream velocity magnitude in m/s
AoA = 6.  # Angle of Attack in degrees
AoA_rad = np.deg2rad(AoA)  # Angle of Attack converted to radians
rho = 1.225  # International Standard Atmosphere air density at sea level in kg/m^3

conv_eps = 1e-6  # Convergence tolerance for iterative solution approach
iterating = True



#######################################################
############## The optimization problem ###############
#######################################################
fea = FEA(solid_mesh)
fea.PDE_SOLVER = "Newton"
fea.initialize = True
fea.record = False
fea.linear_problem = True
# Add input to the PDE problem:
input_name_1 = 'thickness'
input_function_space_1 = FunctionSpace(solid_mesh, ("CG", 1))
# input_function_space_1 = FunctionSpace(solid_mesh, ("DG", 0))
input_function_1 = Function(input_function_space_1)
# Add input to the PDE problem:
input_name_2 = 'F_solid'
input_function_space_2 = VectorFunctionSpace(solid_mesh, ("CG", 1))
input_function_2 = Function(input_function_space_2)

# Add state to the PDE problem:
state_name = 'disp_solid'
state_function_space = element.W
state_function = Function(state_function_space)
g = Function(state_function_space)
with g.vector.localForm() as uloc:
     uloc.set(0.)
# Simple isotropic material
material_model = MaterialModel(E=E,nu=nu,h=input_function_1)
residual_form = pdeRes(input_function_1,state_function,
                        E,input_function_2,material_model.CLT,dx_inplane,dx_shear,
                        penalty=PENALTY_BC, dss=ds_1(100), dSS=dS_1(100), g=g)

# Add output to the PDE problem:
output_name_1 = 'compliance'
u_mid, theta = split(state_function)
# output_form_1 = compliance(u_mid,input_function_1, dx_2(10))
output_form_1 = new_compliance(u_mid,input_function_2, ufl.dx)
output_name_2 = 'volume'
output_form_2 = volume(input_function_1)
output_name_3 = 'elastic_energy'
output_form_3 = elastic_energy(state_function,material_model.CLT,E,input_function_1,dx_inplane,dx_shear)

with input_function_1.vector.localForm() as uloc:
     uloc.set(h_val)
V0 = assemble(output_form_2)

fea.add_input(input_name_1, input_function_1)
fea.add_input(input_name_2, input_function_2)
fea.add_state(name=state_name,
                function=state_function,
                residual_form=residual_form,
                arguments=[input_name_1, input_name_2])
# fea.add_output(name=output_name_1,
#                 type='scalar',
#                 form=output_form_1,
#                 arguments=[state_name,input_name_1])
fea.add_output(name=output_name_1,
                type='scalar',
                form=output_form_1,
                arguments=[state_name,input_name_1,input_name_2])
fea.add_output(name=output_name_2,
                type='scalar',
                form=output_form_2,
                arguments=[input_name_1])
fea.add_output(name=output_name_3,
                type='scalar',
                form=output_form_3,
                arguments=[input_name_1,state_name])
############ Set the BCs for the airplane model ###################

locate_BC1 = locate_dofs_geometrical((state_function_space.sub(0),
                                    state_function_space.sub(0).collapse()[0]),
                                    lambda x: np.less(x[1], y_bc))
locate_BC2 = locate_dofs_geometrical((state_function_space.sub(1),
                                    state_function_space.sub(1).collapse()[0]),
                                    lambda x: np.less(x[1], y_bc))
ubc = Function(state_function_space)
with ubc.vector.localForm() as uloc:
     uloc.set(0.)

############ Strongly enforced boundary conditions #############
if not PENALTY_BC:
    fea.add_strong_bc(ubc, [locate_BC1], state_function_space.sub(0))
    fea.add_strong_bc(ubc, [locate_BC2], state_function_space.sub(1))

################### Construct Aerodynamic mesh ###################
print("Constructing aerodynamic mesh and mesh mappings...")
# Import a preconstructed vlm mesh
vlm_mesh = load_mesh(vlm_mesh_file, np.array([4.28, 0., 2.96]))
vlm_mesh_mirrored = mirror_mesh_around_y_axis(vlm_mesh)
vlm_mesh_baseline_2d = reshape_3D_array_to_2D(vlm_mesh)

vlm_mesh_baseline_2d_mirrored = reshape_3D_array_to_2D(
                                            vlm_mesh_mirrored)

####### Define force functions and aero-elastic coupling object ########
coupling_obj = FEniCSx_vortexmethod_coupling(solid_mesh, vlm_mesh, 
                    state_function_space, RBF_width_par=2.)

# vlm_mesh_displaced_mirrored = deepcopy(vlm_mesh_coordlist_mirrored_baseline)
vlm_mesh_displaced_2d_mirrored = deepcopy(vlm_mesh_baseline_2d_mirrored)
vlm_mesh_displaced_3d_mirrored = np.reshape(vlm_mesh_displaced_2d_mirrored, 
                                (vlm_mesh_mirrored.shape[0], vlm_mesh_mirrored.shape[1], 3), 
                                order='F')
vlm_mesh_transposed = construct_VLM_transposed_input_mesh(vlm_mesh_displaced_3d_mirrored)

# Define CSDL mapping models for force and displacement input/output management
panel_forces_shape = ((vlm_mesh_mirrored.shape[0]-1)*(vlm_mesh_mirrored.shape[1]-1), vlm_mesh_mirrored.shape[2])
panel_forces_3d_shape = ((vlm_mesh_mirrored.shape[0]-1), (vlm_mesh_mirrored.shape[1]-1), vlm_mesh_mirrored.shape[2])
starboard_panel_forces_3d_shape = ((vlm_mesh.shape[0]-1), (vlm_mesh.shape[1]-1), vlm_mesh.shape[2],)
starboard_panel_forces_shape = ((vlm_mesh.shape[0]-1)*(vlm_mesh.shape[1]-1), vlm_mesh.shape[2],)
panel_force_vector_shape = ((vlm_mesh.shape[0]-1)*(vlm_mesh.shape[1]-1)*vlm_mesh.shape[2],)

# Input: 'vlm_mesh_displaced' 
# Output: 'panel_forces'
vlm_class = VLM_CADDEE([vlm_mesh_transposed], AoA,
                V_inf*np.array([np.cos(AoA_rad), 0., np.sin(AoA_rad)]),
                rho=rho)
vlm_model = vlm_class.model # A CSDL model contains VLM as the submodel

vlm_force_reshape_model = VLMForceIOModel(input_name='panel_forces', 
                                        output_name_2d_array='starboard_panel_force_array', 
                                        output_name_vector='F_aero',
                                        input_shape=panel_forces_shape, 
                                        starboard_3d_shape=starboard_panel_forces_3d_shape, 
                                        output_vector_length=panel_force_vector_shape,
                                        full_3d_shape=panel_forces_3d_shape)

force_map_model = ForceMappingModel(coupling=coupling_obj, 
                                        input_name='F_aero', 
                                        state_name='F_solid',
                                        input_shape=(coupling_obj.P_map.shape[1]*3,), 
                                        output_shape=(coupling_obj.Mat_f_sp.shape[0],))

# Input:'F_solid' 
# Output:'disp_solid'
# solid_model = FEAModel(fea=[fea])
solid_model = StateModel(fea=fea,
                        debug_mode=False,
                        state_name=state_name,
                        arg_name_list=fea.states_dict[state_name]['arguments'])
compliance_model = OutputModel(fea=fea,
                            output_name=output_name_1,
                            arg_name_list=fea.outputs_dict[output_name_1]['arguments'])
volume_model = OutputModel(fea=fea,
                            output_name=output_name_2,
                            arg_name_list=fea.outputs_dict[output_name_2]['arguments'])
elastic_energy_model = OutputModel(fea=fea,
                            output_name=output_name_3,
                            arg_name_list=fea.outputs_dict[output_name_3]['arguments'])
# Define CSDL mapping models for aeroelastic coupling
# disp_map_model = DisplacementMappingModel(coupling=coupling_obj, 
#                                         input_name='disp_solid', 
#                                         output_name='disp_fluid', 
#                                         input_shape=(state_function.vector.size,), 
#                                         output_shape=(vlm_mesh.shape[0]*vlm_mesh.shape[1]*vlm_mesh.shape[2],))

disp_map_model = DisplacementMappingImplicitModel(coupling=coupling_obj, 
                                        input_name='disp_solid', 
                                        state_name='disp_fluid',
                                        output_name='r_disp_fluid', 
                                        input_shape=(state_function.vector.size,), 
                                        output_shape=(vlm_mesh.shape[0]*vlm_mesh.shape[1]*vlm_mesh.shape[2],))

vlm_mesh_update_model = VLMMeshUpdateModel(base_vlm_mesh_2d=vlm_mesh_baseline_2d,
                                         starboard_mesh_3d_shape=vlm_mesh.shape,
                                         input_name='disp_fluid', 
                                         output_name_2d='vlm_disp_mat', 
                                        #  output_name_3d='vlm_mesh_displaced',
                                         output_name_3d = 'surf_0',
                                         input_shape=(vlm_mesh.shape[0]*vlm_mesh.shape[1]*vlm_mesh.shape[2],), 
                                         output_shape=vlm_mesh_transposed.shape)

'''
4. Set up the CSDL model
'''
disp_map_model.add(vlm_model, name='vlm_model')
disp_map_model.add(vlm_force_reshape_model, name='vlm_force_reshape_model')
disp_map_model.add(force_map_model, name='force_map_model')
disp_map_model.add(solid_model, name='solid_model')
disp_map_model.add(vlm_mesh_update_model, name='vlm_mesh_update_model')
disp_map_model.declare_variable('thickness',
                    shape=fea.inputs_dict[input_name_1]['shape'],
                    val=h_val)
model = csdl.Model()

solve_fixed_point_iteration = model.create_implicit_operation(disp_map_model)
solve_fixed_point_iteration.declare_state('disp_fluid', residual='r_disp_fluid')
solve_fixed_point_iteration.nonlinear_solver = csdl.NonlinearBlockGS(
                                                maxiter=100, atol=1e-6, rtol=1e-6)
thickness = model.declare_variable('thickness',
                    shape=fea.inputs_dict[input_name_1]['shape'],
                    val=h_val)
x = solve_fixed_point_iteration(thickness)


model.add(vlm_mesh_update_model, name='vlm_mesh_update_model')
model.add(vlm_model, name='vlm_model')
model.add(vlm_force_reshape_model, name='vlm_force_reshape_model')
model.add(force_map_model, name='force_map_model')
model.add(solid_model, name='solid_model')
model.add(compliance_model, name='compliance_model')
model.add(volume_model, name='volume_model')
model.add(elastic_energy_model, name='elastic_energy_model')

# model.create_input('thickness',
#                     shape=fea.inputs_dict[input_name_1]['shape'],
#                     val=h_val)

# model.add_design_variable('thickness', upper=2*h_val, lower=0.1*h_val)
# model.add_objective('compliance')
# model.add_constraint('volume', equals=V0)
sim = Simulator(model)

# ########### Test the forward solve ##############

sim.run()

########## Output: ##############
dofs = len(state_function.vector.getArray())
uZ = computeNodalDisp(state_function.sub(0))[2]
print("-"*50)
print("-"*8, s_mesh_file_name, "-"*9)
print("-"*50)
print("Tip deflection: ", max(uZ))
print("disp_fluid:", max(sim['disp_fluid']))
print("Elastic energy:", sim['elastic_energy'])
print("Compliance: ", sim['compliance'])
print("Initial volume: ", V0)
print("Volume: ", sim['volume'])
print("  Number of elements = "+str(nel))
print("  Number of vertices = "+str(nn))
print("  Number of total dofs = ", dofs)
print("-"*50)

path = "solutions"+"_penalty_bc_"+str(y_bc)
# ########## Compute the total derivatives ###########
@profile(filename="profile_out")
def main(sim):
    
    import timeit
    start = timeit.default_timer()
    error_dict = sim.check_totals(of=['compliance'], wrt=['thickness'], compact_print=True)
    stop = timeit.default_timer()

    dCdT = error_dict[('compliance', 'thickness')]['analytical_jac']
    dCdT_fd = error_dict[('compliance', 'thickness')]['fd_jac']
    dCdT_error = error_dict[('compliance', 'thickness')]['error_jac']
    dCdT_function = Function(input_function_space_1)
    dCdT_function.vector.setArray(dCdT)
    dCdT_fd_function = Function(input_function_space_1)
    dCdT_fd_function.vector.setArray(dCdT_fd)
    dCdT_error_function = Function(input_function_space_1)
    dCdT_error_function.vector.setArray(dCdT_error)

    if MPI.COMM_WORLD.Get_rank() == 0:
        with XDMFFile(MPI.COMM_WORLD, path+"/gradient_dCdT.xdmf", "w") as xdmf:
            xdmf.write_mesh(solid_mesh)
            xdmf.write_function(dCdT_function)
        with XDMFFile(MPI.COMM_WORLD, path+"/gradient_dCdT_fd.xdmf", "w") as xdmf:
            xdmf.write_mesh(solid_mesh)
            xdmf.write_function(dCdT_fd_function)
        with XDMFFile(MPI.COMM_WORLD, path+"/gradient_dCdT_error.xdmf", "w") as xdmf:
            xdmf.write_mesh(solid_mesh)
            xdmf.write_function(dCdT_error_function)

cProfile.run('main(sim)', "profile_out")

########## Visualization: ##############
u_mid, _ = state_function.split()

shell_stress_RM = ShellStressRM(solid_mesh, state_function, h_val, E, nu)
von_Mises_top = shell_stress_RM.vonMisesStress(h_val/2)
V1 = FunctionSpace(solid_mesh, ('CG', 1))
von_Mises_top_func = Function(V1)
project(von_Mises_top, von_Mises_top_func, lump_mass=True)

with XDMFFile(MPI.COMM_WORLD, path+"/u_mid_tri_"+str(dofs)+".xdmf", "w") as xdmf:
    xdmf.write_mesh(solid_mesh)
    xdmf.write_function(u_mid)

with XDMFFile(MPI.COMM_WORLD, path+"/aero_F_"+str(dofs)+".xdmf", "w") as xdmf:
    xdmf.write_mesh(solid_mesh)
    xdmf.write_function(input_function_2)

with XDMFFile(MPI.COMM_WORLD, path+"/von_mises_stress.xdmf", "w") as xdmf:
    xdmf.write_mesh(solid_mesh)
    xdmf.write_function(von_Mises_top_func)

