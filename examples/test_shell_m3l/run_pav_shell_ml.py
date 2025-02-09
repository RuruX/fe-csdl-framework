## Caddee
from caddee.utils.aircraft_models.pav.pav_geom_mesh import PavGeomMesh
import caddee.api as cd

## Solvers
from VAST.core.vast_solver import VASTFluidSover
from VAST.core.fluid_problem import FluidProblem
from VAST.core.generate_mappings_m3l import VASTNodalForces
from VAST.core.vlm_llt.viscous_correction import ViscousCorrectionModel
############## Add these lines to avoid importing error of lsdo_airfoil ############
import os
os.environ['KMP_DUPLICATE_LIB_OK']='True'
####################################################################################
from lsdo_airfoil.core.pressure_profile import PressureProfile, NodalPressureProfile, NodalForces
import dolfinx
from femo.fea.utils_dolfinx import *
import shell_module as rmshell
from shell_pde import ShellPDE

# Other lsdo lab stuff
import csdl
from python_csdl_backend import Simulator
from modopt import SLSQP
from modopt import CSDLProblem
import m3l
from m3l.utils.utils import index_functions
import lsdo_geo as lg
import array_mapper as am
from m3l.core.function_spaces import IDWFunctionSpace


## Other stuff
import numpy as np
from mpi4py import MPI
import pickle
import pathlib
import sys

sys.setrecursionlimit(100000)

do_ML = True
debug_geom_flag = False
force_reprojection = False
visualize_flag = False
dashboard = False
xdmf_record = True

ft2m = 0.3048
in2m = 0.0254

# wing_cl0 = 0.3366
# pitch_angle_list = [-0.02403544, 6, 12.48100761]
# h_0 = 0.02*in2m

wing_cl0 = 0.3662
pitch_angle_list = [-0.38129494, 6, 12.11391141]
h_0 = 0.02*in2m
pitch_angle = np.deg2rad(pitch_angle_list[2])


caddee = cd.CADDEE()
caddee.system_model = system_model = cd.SystemModel()

# region Geometry and meshes
pav_geom_mesh = PavGeomMesh()
pav_geom_mesh.setup_geometry(
    include_wing_flag=True,
    include_htail_flag=False,
)
pav_geom_mesh.setup_internal_wingbox_geometry(debug_geom_flag=debug_geom_flag,
                                              force_reprojection=force_reprojection)
pav_geom_mesh.sys_rep.spatial_representation.assemble()
pav_geom_mesh.oml_mesh(include_wing_flag=True,
                       debug_geom_flag=debug_geom_flag, force_reprojection=force_reprojection)
pav_geom_mesh.setup_index_functions()
pav_geom_mesh.vlm_meshes(include_wing_flag=True, num_wing_spanwise_vlm=41, num_wing_chordwise_vlm=2,
                         visualize_flag=visualize_flag, force_reprojection=force_reprojection, ml=do_ML)
# pav_geom_mesh.setup_index_functions()

caddee.system_representation = sys_rep = pav_geom_mesh.sys_rep
caddee.system_parameterization = sys_param = pav_geom_mesh.sys_param
sys_param.setup()
spatial_rep = sys_rep.spatial_representation
# endregion

# region FEniCS
#############################################
# filename = "./pav_wing/pav_wing_v2_caddee_mesh_SI_6307_quad.xdmf"
filename = "./pav_wing/pav_wing_v2_caddee_mesh_SI_2303_quad.xdmf"

with dolfinx.io.XDMFFile(MPI.COMM_WORLD, filename, "r") as xdmf:
    fenics_mesh = xdmf.read_mesh(name="Grid")
nel = fenics_mesh.topology.index_map(fenics_mesh.topology.dim).size_local
nn = fenics_mesh.topology.index_map(0).size_local

nodes = fenics_mesh.geometry.x


with open('./pav_wing/pav_wing_v2_paneled_mesh_data_'+str(nodes.shape[0])+'.pickle', 'rb') as f:
    nodes_parametric = pickle.load(f)

for i in range(len(nodes_parametric)):
    nodes_parametric[i] = (nodes_parametric[i][0].replace(' ', '_').replace(',',''), np.array([nodes_parametric[i][1]]))

wing_thickness = pav_geom_mesh.functions['wing_thickness']
thickness_nodes = wing_thickness.evaluate(nodes_parametric)

shell_pde = ShellPDE(fenics_mesh)


# Unstiffened Aluminum 2024 (T4)
# reference: https://asm.matweb.com/search/SpecificMaterial.asp?bassnum=ma2024t4
E = 73.1E9 # unit: Pa
nu = 0.33
h = h_0
rho = 2780 # unit: kg/m^3
f_d = -rho*h*9.81 # self-weight unit: N
tensile_yield_strength = 324E6 # unit: Pa
safety_factor = 1.5


y_bc = -1e-6
semispan = pav_geom_mesh.geom_data['points']['wing']['l_tip_te'][1] + 0.001

G = E/2/(1+nu)

#### Getting facets of the LEFT and the RIGHT edge  ####
DOLFIN_EPS = 3E-16
def ClampedBoundary(x):
    return np.greater(x[1], y_bc)
def TipChar(x):
    return np.less(x[1], semispan)
fdim = fenics_mesh.topology.dim - 1

ds_1 = createCustomMeasure(fenics_mesh, fdim, ClampedBoundary, measure='ds', tag=100)
dS_1 = createCustomMeasure(fenics_mesh, fdim, ClampedBoundary, measure='dS', tag=100)
dx_2 = createCustomMeasure(fenics_mesh, fdim+1, TipChar, measure='dx', tag=10)

g = Function(shell_pde.W)
with g.vector.localForm() as uloc:
     uloc.set(0.)

###################  m3l ########################

# create the shell dictionaries:
shells = {}
shells['wing_shell'] = {'E': E, 'nu': nu, 'rho': rho,# material properties
                        'dss': ds_1(100), # custom integrator: ds measure
                        'dSS': dS_1(100), # custom integrator: dS measure
                        'dxx': dx_2(10),  # custom integrator: dx measure
                        'g': g,
                        'record': xdmf_record}


################# PAV  Wing #################

# Wing shell Mesh
z_offset = 0.0
wing_shell_mesh = am.MappedArray(input=fenics_mesh.geometry.x).reshape((-1,3))
shell_mesh = rmshell.LinearShellMesh(
                    meshes=dict(
                    wing_shell_mesh=wing_shell_mesh,
                    ))


# endregion



# region Mission
design_scenario_name = 'structural_sizing'
design_scenario = cd.DesignScenario(name=design_scenario_name)
# endregion

# region Cruise condition
cruise_name = "cruise_3"
cruise_model = m3l.Model()
cruise_condition = cd.CruiseCondition(name=cruise_name)
cruise_condition.atmosphere_model = cd.SimpleAtmosphereModel()
cruise_condition.set_module_input(name='altitude', val=600 * ft2m)
cruise_condition.set_module_input(name='mach_number', val=0.145972)  # 112 mph = 0.145972 Mach
cruise_condition.set_module_input(name='range', val=80467.2)  # 50 miles = 80467.2 m
cruise_condition.set_module_input(name='pitch_angle', val=pitch_angle)
cruise_condition.set_module_input(name='flight_path_angle', val=0)
cruise_condition.set_module_input(name='roll_angle', val=0)
cruise_condition.set_module_input(name='yaw_angle', val=0)
cruise_condition.set_module_input(name='wind_angle', val=0)
cruise_condition.set_module_input(name='observer_location', val=np.array([0, 0, 600 * ft2m]))

cruise_ac_states = cruise_condition.evaluate_ac_states()
cruise_model.register_output(cruise_ac_states)
# endregion

# region VLM Solver
vlm_model = VASTFluidSover(
    surface_names=[
        pav_geom_mesh.mesh_data['vlm']['mesh_name']['wing'],
    ],
    surface_shapes=[
        (1,) + pav_geom_mesh.mesh_data['vlm']['chamber_surface']['wing'].evaluate().shape[1:],
        ],
    fluid_problem=FluidProblem(solver_option='VLM', problem_type='fixed_wake'),
    mesh_unit='m',
    cl0=[wing_cl0, ],
    ML = do_ML
)

if do_ML:
    cl_distribution, re_spans, wing_vlm_panel_forces, panel_areas, evaluation_pt, vlm_forces, vlm_moments = vlm_model.evaluate(ac_states=cruise_ac_states)
else: 
    wing_vlm_panel_forces, vlm_forces, vlm_moments = vlm_model.evaluate(ac_states=cruise_ac_states)
cruise_model.register_output(vlm_forces)
cruise_model.register_output(vlm_moments)
# endregion

# region ML

wing_force = pav_geom_mesh.functions['wing_force']
wing_cp = pav_geom_mesh.functions['wing_cp']

if do_ML:
    ml_pressures = PressureProfile(
        airfoil_name='NASA_langley_ga_1',
        use_inverse_cl_map=True,
    )

    cp_upper, cp_lower, Cd = ml_pressures.evaluate(cl_distribution, re_spans) #, mach_number, reynolds_number)
    cruise_model.register_output(cp_upper)
    cruise_model.register_output(cp_lower)

    wing_vlm_mesh_name = pav_geom_mesh.mesh_data['vlm']['mesh_name']['wing']
    wing_camber_surface = pav_geom_mesh.mesh_data['vlm']['chamber_surface']['wing']
    wing_upper_surface_ml = pav_geom_mesh.mesh_data['ml']['wing_upper']
    wing_lower_surface_ml = pav_geom_mesh.mesh_data['ml']['wing_lower']
    wing_oml_geo = pav_geom_mesh.functions['wing_geo']

    viscous_drag_correction = ViscousCorrectionModel(
        surface_names=[
            f'{wing_vlm_mesh_name}_cruise',
        ],
        surface_shapes=[
            (1, ) + wing_camber_surface.evaluate().shape[1:],
        ],
    )
    moment_point = None
    vlm_F, vlm_M = viscous_drag_correction.evaluate(ac_states=cruise_ac_states, 
                                                    forces=wing_vlm_panel_forces, 
                                                    cd_v=Cd, 
                                                    panel_area=panel_areas, 
                                                    moment_pt=moment_point, 
                                                    evaluation_pt=evaluation_pt, 
                                                    design_condition=cruise_condition)
    cruise_model.register_output(vlm_F)
    cruise_model.register_output(vlm_M)

    ml_pressures_oml_map = NodalPressureProfile(
        surface_names=[
            f'{wing_vlm_mesh_name}_cruise',
        ],
        surface_shapes=[
            wing_upper_surface_ml.value.shape,
        ]
    )

    cp_upper_oml, cp_lower_oml = ml_pressures_oml_map.evaluate(cp_upper, cp_lower, nodal_pressure_mesh=[])
    wing_oml_pressure_upper = cp_upper_oml[0]
    wing_oml_pressure_lower = cp_lower_oml[0]

    upper_normals_ml = pav_geom_mesh.mesh_data['ml']['wing_upper_normals']
    lower_normals_ml = pav_geom_mesh.mesh_data['ml']['wing_lower_normals']
    wing_upper_surface_ml_2 = pav_geom_mesh.mesh_data['ml']['wing_upper_vlm']
    wing_lower_surface_ml_2 = pav_geom_mesh.mesh_data['ml']['wing_lower_vlm']
    # print(wing_upper_surface_ml.shape)
    # print(wing_upper_surface_ml_2.shape)
    # exit()


    ml_nodal_force_map = NodalForces()
    ml_f_upper, ml_f_lower = ml_nodal_force_map.evaluate(vlm_F = vlm_F,
                                                         oml_pressures_upper=wing_oml_pressure_upper, 
                                                         oml_pressures_lower=wing_oml_pressure_lower, 
                                                         normals_upper=upper_normals_ml, 
                                                         normals_lower=lower_normals_ml, 
                                                         upper_ml_mesh=wing_upper_surface_ml,
                                                         lower_ml_mesh=wing_lower_surface_ml,
                                                         upper_ml_vlm_mesh=wing_upper_surface_ml_2,
                                                         lower_ml_vlm_mesh=wing_lower_surface_ml_2)

    cruise_model.register_output(ml_f_upper)
    cruise_model.register_output(ml_f_lower)

    # ml_parametric_nodes = wing_upper_surface_ml_dict['parametric_coordinates']+wing_lower_surface_ml_dict['parametric_coordinates']

    vstack = m3l.VStack()
    wing_oml_force = vstack.evaluate(ml_f_upper, ml_f_lower)
    
    vstack = m3l.VStack()
    wing_oml_pressure = vstack.evaluate(wing_oml_pressure_upper, wing_oml_pressure_lower)

    cruise_model.register_output(wing_oml_pressure_upper)
    cruise_model.register_output(wing_oml_pressure_lower)

    valid_surfaces_ml = pav_geom_mesh.mesh_data['ml']['wing_valid_surfaces']
    wing_upper_surface_parametric = pav_geom_mesh.mesh_data['ml']['wing_upper_parametric']
    wing_lower_surface_parametric = pav_geom_mesh.mesh_data['ml']['wing_lower_parametric']
    ml_nodes_parametric = wing_upper_surface_parametric + wing_lower_surface_parametric
    
    coefficients = {}
    spaces = {}
    for name in wing_force.coefficients.keys():
        if name in valid_surfaces_ml:
            coefficients[name] = wing_force.coefficients[name]
            spaces[name] = wing_force.space.spaces[name]
    wing_force.coefficients = coefficients
    wing_force.space.spaces = spaces

    coefficients = {}
    spaces = {}
    for name in wing_cp.coefficients.keys():
        if name in valid_surfaces_ml:
            coefficients[name] = wing_cp.coefficients[name]
            spaces[name] = wing_cp.space.spaces[name]
    wing_cp.coefficients = coefficients
    wing_cp.space.spaces = spaces

    wing_oml_mesh_ml = am.vstack((wing_upper_surface_ml, wing_lower_surface_ml))
    num_ml_points = 100
    num_spanwise_ml = 40
    ml_nodes = wing_oml_mesh_ml.value.reshape((num_ml_points*2*num_spanwise_ml, 3), order='F')
    ml_nodes_correct = np.zeros(ml_nodes.shape)
    for i in range(num_spanwise_ml):
        ml_nodes_correct[i*100:i*100+100] = ml_nodes[i*200:i*200+100]
        ml_nodes_correct[i*100+100*num_spanwise_ml:i*100+100*num_spanwise_ml+100] = ml_nodes[i*200+100:i*200+200]
    ml_nodes = ml_nodes_correct
    wing_oml = pav_geom_mesh.geom_data['components']['wing_oml']
    ml_nodes_parametric = wing_oml.project(ml_nodes, properties=['parametric_coordinates'], force_reprojection=force_reprojection)
    
    
    wing_force.inverse_evaluate(ml_nodes_parametric, wing_oml_force)
    cruise_model.register_output(wing_force.coefficients)

    wing_cp.inverse_evaluate(ml_nodes_parametric, wing_oml_pressure, regularization_coeff = 1e-3)
    cruise_model.register_output(wing_cp.coefficients)
# endregion


# region VLM forces
vlm_force_mapping_model = VASTNodalForces(
    surface_names=[
        pav_geom_mesh.mesh_data['vlm']['mesh_name']['wing'],
    ],
    surface_shapes=[
        (1,) + pav_geom_mesh.mesh_data['vlm']['chamber_surface']['wing'].evaluate().shape[1:],
        ],
    initial_meshes=[
        pav_geom_mesh.mesh_data['vlm']['chamber_surface']['wing'],
    ]
)

wing_oml_mesh = pav_geom_mesh.mesh_data['oml']['oml_geo_nodes']['wing']
oml_forces = vlm_force_mapping_model.evaluate(vlm_forces=wing_vlm_panel_forces,
                                              nodal_force_meshes=[wing_oml_mesh, ])
wing_forces = oml_forces[0]

# endregion

# region Strucutral Loads

oml_para_nodes = pav_geom_mesh.mesh_data['oml']['oml_para_nodes']['wing']

if not do_ML:
    wing_force.inverse_evaluate(oml_para_nodes, wing_forces)
    cruise_model.register_output(wing_force.coefficients)

left_wing_oml_para_coords = pav_geom_mesh.mesh_data['oml']['oml_para_nodes']['left_wing']
left_oml_geo_nodes = spatial_rep.evaluate_parametric(left_wing_oml_para_coords)

if do_ML:
    valid_surfaces_ml = pav_geom_mesh.mesh_data['ml']['wing_valid_surfaces']
    left_wing_surfaces = pav_geom_mesh.geom_data['primitive_names']['left_wing']
    
    right_wing_surfaces = pav_geom_mesh.geom_data['primitive_names']['right_wing']
    ml_para_mesh = []
    valid_surfaces_ml_left_wing = []
    valid_surfaces_ml_right_wing = []
    for name in valid_surfaces_ml:
        if name in right_wing_surfaces:
            valid_surfaces_ml_right_wing.append(name)
    num = 10
    for name in valid_surfaces_ml:
        if name in left_wing_surfaces:
            valid_surfaces_ml_left_wing.append(name)
            for u in np.linspace(0,1,10):
                for v in np.linspace(0,1,10):
                    ml_para_mesh.append((name, np.array([[u,v]])))
    ml_geo_mesh = spatial_rep.evaluate_parametric(ml_para_mesh)
    
    # print(valid_surfaces_ml_left_wing)
    
    # print(valid_surfaces_ml_right_wing)
#     wing_upper_surface_parametric = pav_geom_mesh.mesh_data['ml']['wing_upper_parametric']
#     wing_lower_surface_parametric = pav_geom_mesh.mesh_data['ml']['wing_lower_parametric']
#     ml_nodes_parametric = wing_upper_surface_parametric + wing_lower_surface_parametric

left_wing_forces = wing_force.evaluate(ml_para_mesh)
wing_component = pav_geom_mesh.geom_data['components']['wing']

shell_force_map_model = rmshell.RMShellForces(component=wing_component,
                                                mesh=shell_mesh,
                                                pde=shell_pde,
                                                shells=shells)
cruise_structural_wing_mesh_forces = shell_force_map_model.evaluate(
                        nodal_forces=left_wing_forces,
                        nodal_forces_mesh=ml_geo_mesh)
# endregion

# region Structures

shell_displacements_model = rmshell.RMShell(component=wing_component,
                                            mesh=shell_mesh,
                                            pde=shell_pde,
                                            shells=shells)

cruise_structural_wing_mesh_displacements, _, cruise_structural_wing_mesh_stresses, wing_mass = \
                                shell_displacements_model.evaluate(
                                    forces=cruise_structural_wing_mesh_forces,
                                    thicknesses=thickness_nodes)
cruise_model.register_output(cruise_structural_wing_mesh_stresses)
cruise_model.register_output(cruise_structural_wing_mesh_displacements)
cruise_model.register_output(wing_mass)

# endregion

# region Nodal Displacements

grid_num = 10
transfer_para_mesh = []
structural_left_wing_names = pav_geom_mesh.geom_data['primitive_names']['structural_left_wing_names']
for name in structural_left_wing_names:
    for u in np.linspace(0,1,grid_num):
        for v in np.linspace(0,1,grid_num):
            transfer_para_mesh.append((name, np.array([u,v]).reshape((1,2))))

transfer_geo_nodes_ma = spatial_rep.evaluate_parametric(transfer_para_mesh)


shell_nodal_displacements_model = rmshell.RMShellNodalDisplacements(component=wing_component,
                                                                    mesh=shell_mesh,
                                                                    pde=shell_pde,
                                                                    shells=shells)
nodal_displacements, tip_displacement = shell_nodal_displacements_model.evaluate(cruise_structural_wing_mesh_displacements, transfer_geo_nodes_ma)
wing_displacement = pav_geom_mesh.functions['wing_displacement']

wing_displacement.inverse_evaluate(transfer_para_mesh, nodal_displacements)
cruise_model.register_output(wing_displacement.coefficients)

wing_stress = pav_geom_mesh.functions['wing_stress']
wing_stress.inverse_evaluate(nodes_parametric, cruise_structural_wing_mesh_stresses, regularization_coeff=1e-3)
cruise_model.register_output(wing_stress.coefficients)

cruise_model.register_output(tip_displacement)
cruise_model.register_output(nodal_displacements)

# endregion

# Add cruise m3l model to cruise condition
cruise_condition.add_m3l_model('cruise_model', cruise_model)
# Add design condition to design scenario
design_scenario.add_design_condition(cruise_condition)

system_model.add_design_scenario(design_scenario=design_scenario)

caddee_csdl_model = caddee.assemble_csdl()

system_model_name = 'system_model.'+design_scenario_name+'.'+cruise_name+'.'+cruise_name+'.'


caddee_csdl_model.add_constraint(system_model_name+'Wing_rm_shell_displacement_map.wing_shell_tip_displacement',upper=0.1,scaler=1E1)
caddee_csdl_model.add_constraint(system_model_name+'Wing_rm_shell_model.rm_shell.aggregated_stress_model.wing_shell_aggregated_stress',upper=324E6/1.5,scaler=1E-8)
caddee_csdl_model.add_objective(system_model_name+'Wing_rm_shell_model.rm_shell.mass_model.mass', scaler=1e-1)

# Minimum thickness: 0.02 inch -> 0.000508 m
# Minimum thickness: 0.05 inch 

h_min = h

i = 0
shape = (9, 1)
valid_structural_left_wing_names = structural_left_wing_names

################################################################
#### Full thicknesses: individual for spars, skins and ribs ####
################################################################
for name in valid_structural_left_wing_names:
    primitive = spatial_rep.get_primitives([name])[name].geometry_primitive
    name = name.replace(' ', '_').replace(',','')
    surface_id = i

    h_init = caddee_csdl_model.create_input('wing_thickness_dv_'+name, val=h_min)
    caddee_csdl_model.add_design_variable('wing_thickness_dv_'+name, # 0.05 in
                                          lower=0.005 * in2m,
                                          upper=0.1 * in2m,
                                          scaler=1000,
                                          )
    caddee_csdl_model.register_output('wing_thickness_surface_'+name, csdl.expand(h_init, shape))
    caddee_csdl_model.connect('wing_thickness_surface_'+name,
                                system_model_name+'wing_thickness_function_evaluation.'+\
                                name+'_wing_thickness_coefficients')
    i += 1

if dashboard:
    import lsdo_dash.api as ld
    index_functions_map = {}
    index_functions_surfaces = {}

    index_functions_map['wing_thickness'] = wing_thickness  
    index_functions_map['wing_force'] = wing_force
    index_functions_map['wing_cp'] = wing_cp
    index_functions_map['wing_displacement'] = wing_displacement
    index_functions_map['wing_stress'] = wing_stress
    if do_ML:
        index_functions_surfaces['valid_surfaces_ml_left_wing'] = valid_surfaces_ml_left_wing
        index_functions_surfaces['valid_surfaces_ml_right_wing'] = valid_surfaces_ml_right_wing
    rep = csdl.GraphRepresentation(caddee_csdl_model)

    # profiler.disable()
    # profiler.dump_stats('output')

    caddee_viz = ld.caddee_plotters.CaddeeViz(
        caddee = caddee,
        system_m3l_model = system_model,
        design_configuration_map={},
    )

if __name__ == '__main__':
    if dashboard:
        from dash_pav_ml import TC2DB
        dashbuilder = TC2DB()
        sim = Simulator(rep, analytics=True, dashboard = dashbuilder)
    else:
        sim = Simulator(caddee_csdl_model, analytics=True)

    sim.run()

    # sim.check_totals(of=[system_model_name+'Wing_rm_shell_model.rm_shell.aggregated_stress_model.wing_shell_aggregated_stress'],
    #                                     wrt=['h_spar', 'h_skin', 'h_rib'])

    # sim.check_totals(of=[system_model_name+'Wing_rm_shell_model.rm_shell.mass_model.mass'],
    #                                     wrt=['h_spar', 'h_skin', 'h_rib'])
    ########################## Run optimization ##################################
    prob = CSDLProblem(problem_name='pav', simulator=sim)

    # optimizer = SLSQP(prob, maxiter=50, ftol=1E-5)

    from modopt import SNOPT
    optimizer = SNOPT(prob,
                      Major_iterations = 100,
                      Major_optimality = 1e-5,
                      append2file=True)

    optimizer.solve()
    optimizer.print_results()


    ####### Aerodynamic output ##########
    print("="*60)
    print("="*20+'aerodynamic outputs'+"="*20)
    print("="*60)
    print('Pitch: ', np.rad2deg(
        sim[system_model_name+cruise_name+'_ac_states_operation.'+cruise_name+'_pitch_angle']))
    print('C_L: ', sim[system_model_name+'wing_vlm_mesh_vlm_model.vast.VLMSolverModel.VLM_outputs.LiftDrag.wing_vlm_mesh_C_L'])
    # print('Total lift: ', sim[system_model_name+'wing_vlm_mesh_vlm_model.vast.VLMSolverModel.VLM_outputs.LiftDrag.total_lift'])
    wing_cp_upper = sim['system_model.structural_sizing.cruise_3.cruise_3.airfoil_ml_model.wing_cp_upper']
    print('C_P upper:', np.min(wing_cp_upper), np.max(wing_cp_upper))
    ####### Structural output ##########
    print("="*60)
    print("="*20+'structure outputs'+"="*20)
    print("="*60)
    # Comparing the solution to the Kirchhoff analytical solution
    f_shell = sim[system_model_name+'Wing_rm_shell_force_mapping.wing_shell_forces']
    # f_vlm = sim[system_model_name+'wing_vlm_mesh_vlm_nodal_forces_model.wing_vlm_mesh_oml_forces'].reshape((-1,3))
    u_shell = sim[system_model_name+'Wing_rm_shell_model.rm_shell.disp_extraction_model.wing_shell_displacement']
    u_nodal = sim[system_model_name+'Wing_rm_shell_displacement_map.wing_shell_nodal_displacement']
    u_tip = sim[system_model_name+'Wing_rm_shell_displacement_map.wing_shell_tip_displacement']
    uZ = u_shell[:,2]
    # uZ_nodal = u_nodal[:,2]


    wing_von_Mises_stress = sim[system_model_name+'Wing_rm_shell_model.rm_shell.von_Mises_stress_model.wing_shell_stress']
    wing_mass = sim[system_model_name+'Wing_rm_shell_model.rm_shell.mass_model.mass']
    wing_elastic_energy = sim[system_model_name+'Wing_rm_shell_model.rm_shell.elastic_energy_model.elastic_energy']
    wing_aggregated_stress = sim[system_model_name+'Wing_rm_shell_model.rm_shell.aggregated_stress_model.wing_shell_aggregated_stress']
    ########## Output: ##########
    # print("Spar, rib, skin thicknesses:", sim['h_spar'], sim['h_rib'], sim['h_skin'])

    fz_func = Function(shell_pde.VT)
    fz_func.x.array[:] = f_shell[:,-1]

    fx_func = Function(shell_pde.VT)
    fx_func.x.array[:] = f_shell[:,0]

    fy_func = Function(shell_pde.VT)
    fy_func.x.array[:] = f_shell[:,1]

    dummy_func = Function(shell_pde.VT)
    dummy_func.x.array[:] = 1.0
    # print("vlm forces:", sum(f_vlm[:,0]),sum(f_vlm[:,1]),sum(f_vlm[:,2]))
    print("shell forces:", dolfinx.fem.assemble_scalar(form(fx_func*ufl.dx)),
                            dolfinx.fem.assemble_scalar(form(fy_func*ufl.dx)),
                            dolfinx.fem.assemble_scalar(form(fz_func*ufl.dx)))

    print("Wing surface area:", dolfinx.fem.assemble_scalar(form(dummy_func*ufl.dx)))
    print("Wing tip deflection (m):",max(abs(uZ)))
    print("Wing tip deflection computed by CSDL (m):",np.max(u_nodal))
    print("Wing tip deflection computed by CSDL (m):",u_tip)
    print("Wing total mass (kg):", wing_mass)
    print("Wing aggregated von Mises stress (Pascal):", wing_aggregated_stress)
    print("Wing maximum von Mises stress (Pascal):", max(wing_von_Mises_stress))
    print("  Number of elements = "+str(nel))
    print("  Number of vertices = "+str(nn))

