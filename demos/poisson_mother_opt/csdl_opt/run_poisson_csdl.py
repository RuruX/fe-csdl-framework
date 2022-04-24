
import csdl
from csdl import Model
from csdl_om import Simulator
from matplotlib import pyplot as plt
from fea import *
from states_model import StatesModel
from scalar_output_model import ScalarOutputModel

# import argparse
# parser = argparse.ArgumentParser()
# parser.add_argument('--fea',dest='fea',default='dolfin',
#                     help='FEA backend')

# args = parser.parse_args()
# fea = str(args.fea)
# if fea == 'dolfin':
#     from fea_old_dolfin import *
# elif fea == 'dolfinx':
#     from fea_dolfinx import *
# else:
#     TypeError("Unsupported FEA backend; choose 'dolfin' or 'dolfinx'")

class PoissonModel(Model):
    def initialize(self):
        self.parameters.declare('fea')

    def define(self):
        self.fea = fea = self.parameters['fea']
        
        f = self.create_input('f', shape=(fea.total_dofs_f,), 
                            val=getFuncArray(self.fea.initial_guess_f))

        self.add(StatesModel(fea=self.fea, debug_mode=False), 
                            name='states_model', promotes=[])
        self.add(ScalarOutputModel(fea=self.fea), 
                            name='scalar_output_model', promotes=[])
        self.connect('f', 'states_model.f')
        self.connect('f', 'scalar_output_model.f')
        self.connect('states_model.u', 'scalar_output_model.u')

        self.add_design_variable('f')
        self.add_objective('scalar_output_model.objective')


if __name__ == '__main__':

    num_el = 4
    mesh = createUnitSquareMesh(num_el)
    fea = FEA(mesh)

    f_ex = fea.f_ex
    u_ex = fea.u_ex
    model = PoissonModel(fea=fea)
    sim = Simulator(model)

    fea = model.fea
    # setting the design variable to be the exact solution
    ############## Run the simulation with the exact solution #########
    # sim['f'] = computeArray(f_ex)
#    sim.run()
#    print("="*40)
#    print("Objective value: ", sim['scalar_output_model.objective'])
#    control_error = errorNorm(f_ex, fea.f)
#    print("Error in controls:", control_error)
#    state_error = errorNorm(u_ex, fea.u)
#    print("Error in states:", state_error)
#    plt.figure(1)
#    plot(fea.u)
#    plt.show()

    # TODO: fix the `check_totals`
    # sim.check_partials(compact_print=True)
    # sim.prob.check_totals(compact_print=True)

    # TODO: 
    ############## Run the optimization with pyOptSparse #############
    import openmdao.api as om
    sim.prob.run_model()
    print("Objective value: ", sim['scalar_output_model.objective'])
    # sim.prob.check_totals(compact_print=True)
    ####### Driver = SLSQP #########
#    sim.prob.driver = om.ScipyOptimizeDriver()
#    sim.prob.driver.options['optimizer'] = 'SLSQP'
#    sim.prob.driver.options['tol'] = 1e-12
#    sim.prob.driver.options['disp'] = True

#    sim.prob.run_driver()

    ####### Driver = SNOPT #########
    driver = om.pyOptSparseDriver()
    driver.options['optimizer']='SNOPT'
    driver.opt_settings['Major feasibility tolerance'] = 1e-12
    driver.opt_settings['Major optimality tolerance'] = 1e-13
    driver.options['print_results'] = False
    
    sim.prob.driver = driver
    sim.prob.run_driver()
    
    ############## Output ###################
    print("="*40)
    print("Objective value: ", sim['scalar_output_model.objective'])
    control_error = errorNorm(f_ex, fea.f)
    print("Error in controls:", control_error)
    state_error = errorNorm(u_ex, fea.u)
    print("Error in states:", state_error)
    print("="*40)
    
    ########### Postprocessing with DOLFIN #############
    # plt.figure(1)
    # plot(fea.u)
    # plt.show()
    # File('f_opt_dolfin.pvd') << fea.f
    # File('u_opt_dolfin.pvd') << fea.u

    ########### Postprocessing with DOLFINx #############
    with XDMFFile(MPI.COMM_WORLD, "solutions/u_opt_dolfinx.xdmf", "w") as xdmf:
        xdmf.write_mesh(fea.mesh)
        xdmf.write_function(fea.u)
    with XDMFFile(MPI.COMM_WORLD, "solutions/f_opt_dolfinx.xdmf", "w") as xdmf:
        xdmf.write_mesh(fea.mesh)
        xdmf.write_function(fea.f)

    # TODO: fix the check_first_derivatives()
    ############## Run the optimization with modOpt #############
    # from modopt.csdl_library import CSDLProblem

    # # Instantiate your problem using the csdl Simulator object and name your problem
    # prob = CSDLProblem(
    #     problem_name='poisson-mother',
    #     simulator=sim,
    # )
    
    # from modopt.snopt_library import SNOPT

    # optimizer = SNOPT(  prob, 
    #                     Major_iterations = 100, 
    #                     Major_optimality=1e-12, 
    #                     Major_feasibility=1e-13)

    # from modopt.scipy_library import SLSQP

    # # Setup your preferred optimizer (SLSQP) with the Problem object 
    # # Pass in the options for your chosen optimizer
    # optimizer = SLSQP(prob, maxiter=100)

    # # Check first derivatives at the initial guess, if needed
    # optimizer.check_first_derivatives(prob.x0)
    # # # Solve your optimization problem
    # optimizer.solve()

    # # # Print results of optimization
    # optimizer.print_results()
    
    # optimizer.print_available_outputs()
