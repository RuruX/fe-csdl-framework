"""
A simple thickness optimization problem using shell elements in FEMO
"""

import dolfinx
from mpi4py import MPI
import csdl_alpha as csdl
from femo.rm_shell.rm_shell_model import RMShellModel
from femo.fea.utils_dolfinx import createCustomMeasure
import numpy as np

beam = [#### quad mesh ####
        "plate_2_10_quad_4_20.xdmf",
        "plate_2_10_quad_8_40.xdmf",
        "plate_2_10_quad_10_50.xdmf",]

filename = "./plate_meshes/"+beam[2]
with dolfinx.io.XDMFFile(MPI.COMM_WORLD, filename, "r") as xdmf:
    mesh = xdmf.read_mesh(name="Grid")
nel = mesh.topology.index_map(mesh.topology.dim).size_local
nn = mesh.topology.index_map(0).size_local

E = 4.32e8
nu = 0.0
h = 0.2
rho = 1.0
width = 2.
length = 10.
f_d = 10.*h

#### Getting facets of the LEFT and the RIGHT edge  ####
DOLFIN_EPS = 3E-16
def ClampedBoundary(x):
    return np.less(x[0], 0.0+DOLFIN_EPS)
def TipChar(x):
    return np.greater(x[0], length+DOLFIN_EPS)
fdim = mesh.topology.dim - 1

ds_1 = createCustomMeasure(mesh, fdim, ClampedBoundary, measure='ds', tag=100)
dS_1 = createCustomMeasure(mesh, fdim, ClampedBoundary, measure='dS', tag=100)
dx_2 = createCustomMeasure(mesh, fdim+1, TipChar, measure='dx', tag=10)

###################  m3l ########################

# create the shell dictionaries:
shells = {'E': E, 'nu': nu, 'rho': rho,# material properties
            'dss': ds_1(100), # custom ds measure for the Dirichlet BC
            'dSS': dS_1(100), # custom dS measure for the Dirichlet BC
            'dxx': dx_2(10),
            'record': True}  # custom integrator: dx measure}

recorder = csdl.Recorder(inline=True)
recorder.start()

force_vector = csdl.Variable(value=np.zeros((nn, 3)), name='force_vector')
force_vector.value[:, 2] = f_d
thicknesses = csdl.Variable(value=h*np.ones(nn), name='thicknesses')

shell_model = RMShellModel(mesh, shells)
shell_outputs = shell_model.evaluate(force_vector, thicknesses)

disp_solid = shell_outputs.disp_solid
compliance = shell_outputs.compliance

recorder.stop()

########## Output: ##########

# Comparing the solution to the Kirchhoff analytical solution

Ix = width*h**3/12
print("Euler-Beinoulli Beam theory deflection:",
    float(f_d*width*length**4/(8*E*Ix)))
print("Reissner-Mindlin FE deflection:", max(disp_solid.value))
print("Compliance:", compliance.value)

print("  Number of elements = "+str(nel))
print("  Number of vertices = "+str(nn))

########## Visualization: ##############
w = shell_model.fea.states_dict['disp_solid']['function']
u_mid, _ = w.split()
with dolfinx.io.XDMFFile(MPI.COMM_WORLD, "solutions/u_mid.xdmf", "w") as xdmf:
    xdmf.write_mesh(mesh)
    xdmf.write_function(u_mid)




