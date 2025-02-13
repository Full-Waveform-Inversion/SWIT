###############################################################################
#
# SWIT v1.1: Seismic Waveform Inversion Toolbox
#
#   A Python package for seismic waveform inversion
#   By Haipeng Li at USTC & Stanford
#   Email: haipengl@mail.ustc.edu.cn, haipeng@stanford.edu 
#
#   Workflow mudule defines the implementation of FWI and RTM workflows
#
###############################################################################

import os
import time
from multiprocessing import Pool

import numpy as np
from misfit import calculate_adjoint_misfit_is
from plot import plot_misfit, plot_model, plot_wavelet, plot_waveform_comparison
from tools import load_waveform_data, smooth2d, save_float
from utils import preconditioner


class FWI(object):
    ''' Full Waveform Inversion workflow

    Parameters
    ----------
        solver : object
            solver object
        optimizer : object
            optimizer object
        preprocessor : object
            preprocessor object
    '''

    def __init__(self, solver, optimizer, preprocessor):
        ''' Initialize FWI Workflow
        '''
        # basic parameters
        self.solver = solver
        self.optimizer = optimizer
        self.preprocessor = preprocessor

        # build directories
        self.__build_dir()

        # check the existence of obs data
        self.__check_obs_data()

        # plot the initial model and gradient mask
        self.__plot_initial_model()

        

    def run(self):
        ''' Run FWI Workflow
        '''

        # start the timer
        start_time = time.time()

        # preprocess the obs data
        self.preprocessor.run(data_path = self.solver.system.path + 'data/obs/', 
                            src_num = self.solver.source.num, 
                            mpi_cpu_num = self.solver.system.mpi_cpu_num, 
                            nt = self.solver.model.nt, 
                            dt = self.solver.model.dt,  
                            offset = self.solver.model.offset)

        # compute cost and gradient for the initial model
        vp  = self.optimizer.vp_init
        rho = self.optimizer.rho_init # rho is not updated in FWI Workflow
        fcost, fcost_all, grad_preco = self.objective_function(vp = vp, rho = rho)

        # perform the source inversion using the initial model
        self.source_inversion(tag = 'init')


        # iterate until convergence or linesearch failure
        while ((self.optimizer.FLAG != 'CONV') and (self.optimizer.FLAG != 'FAIL')):

            # update model and linesearch using the preconditioned gradient
            vp_pre = np.copy(vp)
            vp = self.optimizer.iterate(vp, fcost, grad_preco, np.copy(grad_preco))

            # calculate gradient and misfit from updated model
            if(self.optimizer.FLAG == 'GRAD'):
                # print information
                self.print_iterate_info(fcost, vp, vp_pre)

                # compute cost and gradient for the updated model
                fcost, fcost_all, grad_preco = self.objective_function(vp = vp, rho = rho)
                
                # save the iteration history
                self.save_results(vp, grad_preco, fcost_all)

                # plot the model, gradient and misfit
                self.plot_results(vp, grad_preco)

        # perform the source inversion using the final model
        self.source_inversion(tag = 'FWI')

        # print the end information
        self.print_end_info(start_time)


    def objective_function(self, vp = None, rho = None):
        ''' Objective function for FWI

        Parameters
        ----------
        vp : flatted 1D array (float32)
            velocity model
        rho : flatted 1D array (float32)
            density model (optional)
        '''
        
        if vp is None and rho is None:
            msg = 'FWI Workflow ERROR: no model provided for gradient calculation'
            raise ValueError(msg)

        # reset model to solver
        self.solver.set_model(vp = vp, rho = rho)

        # model the syn data (save boundary for wavefield reconstruction)
        self.solver.run(simu_type = 'forward', simu_tag = 'syn', save_boundary = True)

        # preprocess the syn data
        self.preprocessor.run(data_path = self.solver.system.path + 'data/syn/', 
                            src_num = self.solver.source.num, 
                            mpi_cpu_num = self.solver.system.mpi_cpu_num, 
                            nt = self.solver.model.nt, 
                            dt = self.solver.model.dt,  
                            offset = self.solver.model.offset)
        
        # calculate adjoint source and calculate misfit
        fcost, fcost_all = self.calculate_adjoint_misfit()

        # calculate the gradient via adjoint modeling
        grad, for_illum, adj_illum = self.solver.run(simu_type = 'gradient', simu_tag = 'syn')

        # postprocess the gradient
        grad_preco = self.postprocess_gradient(grad, for_illum, adj_illum)

        # save misfit and gradient for each iteration
        return fcost, fcost_all, grad_preco


    def calculate_adjoint_misfit(self):
        ''' Prepare the adjoint source and calculate misfit function

        Returns
        -------
            rsd : list of float
                each element is the summed misfit over all traces for one source
            
            Note: the adjoint source is saved in config/wavelet/src1_adj.bin, ...
        '''

        # initialize the Pool object
        pool = Pool(self.solver.system.mpi_cpu_num)

        # TODO: add the MPI support for the calculation of misfit
        # apply to all sources
        results = [pool.apply_async(calculate_adjoint_misfit_is, (isrc, 
                            self.solver.system.path, 
                            self.solver.model.dt, 
                            self.solver.model.nt, 
                            self.optimizer.misfit_type, ))
                for isrc in range(self.solver.source.num)]

        # close the pool and wait for the work to finish
        pool.close()

        # get the misfits for all sources (not the summed misfit)
        fcost_all = np.array([p.get() for p in results])

        # block at this line until all processes are done
        pool.join()
    
        # return the summed misfit over all sources
        return np.sum(fcost_all), fcost_all


    def postprocess_gradient(self, grad, for_illum, adj_illum):
        ''' Postprocess gradient. 
            Note that the sequence of operations is important here.

        Parameters
        ----------
            grad : 2D array
                gradient of misfit function
            for_illum : 2D array
                forward illumination
            adj_illum : 2D array
                adjoint illumination

        Returns
        -------
        grad : 2D array
            preconditioned gradient of misfit function
        '''

        # mask the gradient
        grad = grad * self.optimizer.grad_mask
   
        # preconditioning the gradient
        grad = grad / np.power(preconditioner(for_illum, adj_illum), 1.)

        # smooth the gradient if needed
        if self.optimizer.grad_smooth_size > 0:
            grad = smooth2d(grad, span = self.optimizer.grad_smooth_size)

        # scale the gradient with the maximum allowed update value
        grad = grad / np.max(abs(grad)) * self.optimizer.update_vpmax

        return grad


    def source_inversion(self, tag = 'init', inv_offset = 10000):
        ''' Perform the source inversion using the obs and syn data

        Parameters
        ----------
            tag : str
                tag for the source inversion
            inv_offset : int
                offset for selecting the source inversion data
        '''
        
        # initialize the wavelet
        wavelet_inverted = np.zeros_like(self.solver.source.wavelet)

        # perform the source inversion for each source
        for isrc in range(self.solver.source.num):
            # get the path of obs and syn data
            obs_path = os.path.join(self.solver.system.path, 'data/obs/src{}/sg_processed'.format(isrc+1))
            syn_path = os.path.join(self.solver.system.path, 'data/syn/src{}/sg_processed'.format(isrc+1))

            # load obs and syn data
            obs, _ = load_waveform_data(obs_path, self.solver.model.nt)
            syn, _ = load_waveform_data(syn_path, self.solver.model.nt)

            # select the data for source inversion based on the offset
            rec = np.argwhere(abs(self.solver.model.offset[isrc]) < inv_offset)
            n1 = int(rec[ 0])
            n2 = int(rec[-1])
            obs = obs[n1:n2, :].T
            syn = syn[n1:n2, :].T

            # perform the fourier transform
            Do = np.fft.fft(obs, axis=0)  # frequency domain
            Dm = np.fft.fft(syn, axis=0)  # frequency domain
        
            src = np.squeeze(self.solver.source.wavelet[isrc, :])
            S = np.fft.fft(np.squeeze(src), axis=0) # frequency domain

            # check if the trace is zero
            if abs(np.sum(Dm * np.conj(Dm))) == 0:
                raise ValueError("FWI Workflow: no trace for source inversion, check for the reason.")
            
            # perform the source inversion
            A = np.sum(np.conj(Dm)*Do, axis=1) / np.sum(Dm * np.conj(Dm), axis=1)
            temp = np.real(np.fft.ifft(A*S[:]))
            wavelet_inverted[isrc, :] = temp / np.max(abs(temp))
        
        # save the inverted wavelet
        np.save(os.path.join(self.solver.system.path, 'fwi/model/wavelet_{}.npy'.format(tag)), wavelet_inverted)
        
        # plot the inverted wavelet
        plot_wavelet(self.solver.source.coord[:,0], 
            self.solver.model.t,
            wavelet_inverted.T, 
            -np.max(abs(wavelet_inverted)), 
            np.max(abs(wavelet_inverted)),
            os.path.join(self.solver.system.path, 'fwi/figures/wavelet_{}.png'.format(tag)), 
            'Inverted wavelet', 
            figaspect = 1, 
            colormap = 'seismic')


    def print_iterate_info(self, fcost, vp, vp_pre):
        ''' Print information during FWI iterations
        '''
        # calculate the maximum update
        max_update = np.max(np.abs(vp-vp_pre.flatten()))

        # print the information
        print('Iteration: {} \t fcost: {:.4e} \t step: {:7.4f} \t max update: {:7.2f} m/s'.format(
                self.optimizer.cpt_iter+1, fcost, self.optimizer.alpha, max_update))
    
    
    def print_end_info(self, start_time):
        ''' Print end information
        '''
        # print the end information
        hours, rem = divmod(time.time()-start_time, 3600)
        minutes, seconds = divmod(rem, 60)
        print('\nFWI Workflow: finished in {:0>2}h {:0>2}m {:.0f}s'.format(int(hours), int(minutes), seconds))
        print('FWI Workflow: see convergence history in iterate_{}.log\n'.format(self.optimizer.method))


    def save_results(self, vp, grad, fcost_all):
        ''' Save results during FWI iterations
        '''
        iter = self.optimizer.cpt_iter+1
        np.save(self.solver.system.path + 'fwi/grad/grad_it_{:04d}.npy'.format(iter), grad)
        np.save(self.solver.system.path + 'fwi/model/vp_it_{:04d}.npy'.format(iter), vp)
        np.save(self.solver.system.path + 'fwi/misfit/fcost_all_it_{:04d}.npy'.format(iter), fcost_all)


    def plot_results(self, vp, grad):
        ''' Plot results during FWI iterations
        '''
        # plot model
        plot_model(self.solver.model.x, 
            self.solver.model.z,
            vp.reshape(self.solver.model.nx, self.solver.model.nz).T, 
            self.optimizer.vp_min, 
            self.optimizer.vp_max,
            os.path.join(self.solver.system.path, 'fwi/figures/vp_it_{:04d}.png'.format(self.optimizer.cpt_iter+1)), 
            'Vp-{:04d}'.format(self.optimizer.cpt_iter+1), 
            figaspect = 1, 
            colormap = 'jet')

        # plot gradient
        plot_model(self.solver.model.x, 
            self.solver.model.z,
            grad.reshape(self.solver.model.nx, self.solver.model.nz).T, 
            -self.optimizer.update_vpmax, 
            self.optimizer.update_vpmax,
            os.path.join(self.solver.system.path, 'fwi/figures/grad_it_{:04d}.png'.format(self.optimizer.cpt_iter+1)), 
            'Gradient-{:04d}'.format(self.optimizer.cpt_iter+1), 
            figaspect = 1, 
            colormap = 'seismic')

        # plot_misfit
        plot_misfit(self.solver.system.path, 
            self.optimizer.method, 
            self.optimizer.cpt_iter+1, 
            self.optimizer.niter_max, 
            self.solver.source.num)

        # plot waveform comparison (every 4 sources)
        for isrc in range(0, self.solver.source.num-1, 4):
            plot_waveform_comparison(self.solver.model.t, self.solver.model.offset[isrc],
                                    self.solver.system.path, 
                                    isrc = isrc+1, 
                                    iter = self.optimizer.cpt_iter + 1)


    def __check_obs_data(self):
        ''' Check the existence of observed data
        '''

        for isrc in range(self.solver.source.num):
            sg_file = self.solver.system.path + 'data/obs/src' + str(isrc+1) + '/sg'
            
            if (not os.path.exists(sg_file + '.segy') and 
                not os.path.exists(sg_file + '.su') and 
                not os.path.exists(sg_file + '.bin')) :
                msg = 'FWI Workflow ERROR: observed data are not found: {}.segy (.su or .bin)'.format(sg_file)
                raise ValueError(msg)

        print('FWI Workflow: find  obs data in {}data/obs/'.format(self.solver.system.path))
        print('FWI Workflow: start iteration ...\n')


    def __build_dir(self):
        ''' Build directories for FWI Workflow and clean up the previous results if any
        '''


        # print the working path
        path = self.solver.system.path
        print('\nFWI Workflow: working path   in {}'.format(path + 'fwi'))

        # build required directories and clean up the previous results if any
        folders = [path + 'fwi', 
                   path + 'fwi/grad',
                   path + 'fwi/model',
                   path + 'fwi/misfit',
                   path + 'fwi/waveform',
                   path + 'fwi/figures',]

        for folder in folders:
            if os.path.exists(folder):
                os.system('rm -rf ' + folder)
                print('FWI Workflow: clean old data in {}'.format(path + 'fwi'))
            os.makedirs(folder)


    def __plot_initial_model(self):
        ''' Plot initial model and gradient mask
        '''

        # plot initial model
        plot_model(self.solver.model.x, 
            self.solver.model.z,
            self.optimizer.vp_init.reshape(self.solver.model.nx, self.solver.model.nz).T, 
            self.optimizer.vp_min, 
            self.optimizer.vp_max,
            os.path.join(self.solver.system.path, 'fwi/figures/vp_it_0000.png'), 
            'Vp-init', 
            figaspect = 1, 
            colormap = 'jet')

        # plot gradient mask on
        plot_model(self.solver.model.x, 
            self.solver.model.z,
            self.optimizer.grad_mask.reshape(self.solver.model.nx, self.solver.model.nz).T, 
            0, 
            1,
            os.path.join(self.solver.system.path, 'fwi/figures/grad_mask.png'), 
            'Gradient mask', 
            figaspect = 1, 
            colormap = 'gray')


class RTM(object):
    ''' Reverse time migration (RTM) class
    '''

    def __init__(self, solver, preprocessor):
        ''' Initialize RTM class

        Parameters
        ----------
        solver : Solver object
            solver object
        preprocessor : Preprocessor object
            preprocessor object
        '''

        self.solver = solver
        self.preprocessor = preprocessor

        # build directories
        self.__build_dir()

        # check the existence of obs data
        self.__check_obs_data()
    

    def __build_dir(self):
        ''' Build directories for RTM workflow and clean up the previous results if any
        '''

        # print the working path
        path = self.solver.system.path

        # build required directories and clean up the previous results if any
        folders = [path + 'rtm', ]

        for folder in folders:
            if os.path.exists(folder):
                os.system('rm -rf ' + folder)
                print('RTM Workflow: clean old data in {}'.format(path + 'rtm'))
            os.makedirs(folder)

    
    def __check_obs_data(self):
        ''' Check the existence of observed data
        '''

        print('\nRTM Workflow: initialization finished')
        for isrc in range(self.solver.source.num):
            sg_file = self.solver.system.path + 'data/obs/src' + str(isrc+1) + '/sg'
            
            if (not os.path.exists(sg_file + '.segy') and 
                not os.path.exists(sg_file + '.su') and 
                not os.path.exists(sg_file + '.bin')) :
                msg = 'RTM Workflow ERROR: observed data are not found: {}.segy (.su or .bin)'.format(sg_file)
                raise ValueError(msg)

        print('RTM Workflow: find  obs data in {}data/obs/'.format(self.solver.system.path))
        print('RTM Workflow: start RTM ...\n')


    def run(self, vp = None, rho = None):
        ''' Run RTM Workflow

        Parameters
        ----------
            vp : 2D array
                velocity model used for RTM
            rho : 2D array
                density model used for RTM

        Notes: in performing RTM, a smooth model is preferred.
        '''

        if vp is None and rho is None:
            print('RTM Workflow: no model is provided for RTM, use the true model')

        # start the timer
        start_time = time.time()

        # preprocess the obs data
        self.preprocessor.run(data_path = self.solver.system.path + 'data/obs/', 
                            src_num = self.solver.source.num, 
                            mpi_cpu_num = self.solver.system.mpi_cpu_num, 
                            nt = self.solver.model.nt, 
                            dt = self.solver.model.dt,  
                            offset = self.solver.model.offset)

        # reset model to solver
        self.solver.set_model(vp = vp, rho = rho)

        # model the syn data (save boundary for wavefield reconstruction)
        self.solver.run(simu_type = 'forward', simu_tag = 'syn', save_boundary = True)

        # preprocess the syn data
        self.preprocessor.run(data_path = self.solver.system.path + 'data/syn/', 
                            src_num = self.solver.source.num, 
                            mpi_cpu_num = self.solver.system.mpi_cpu_num, 
                            nt = self.solver.model.nt, 
                            dt = self.solver.model.dt,  
                            offset = self.solver.model.offset)
        
        # prepare adjoint source for RTM
        for isrc in range(self.solver.source.num):
            obs_path = os.path.join(self.solver.system.path, 'data/obs/src{}/sg_processed'.format(isrc+1))
            adj_path = os.path.join(self.solver.system.path, 'config/wavelet/src{}_adj.bin'.format(isrc+1))
            obs_data, _ = load_waveform_data(obs_path, self.solver.model.nt)
            save_float(adj_path, obs_data)

        # calculate the image via adjoint modeling
        image, for_illum, adj_illum = self.solver.run(simu_type = 'gradient', simu_tag = 'syn')

        # save results
        self.save_results(image, for_illum, adj_illum)

        # plot the results
        self.plot_results(image)

        # print end information
        self.print_end_info(start_time)


    def save_results(self, image, for_illum, adj_illum):
        ''' Save results during FWI iterations
        '''

        np.save(self.solver.system.path + 'rtm/image.npy', image)
        np.save(self.solver.system.path + 'rtm/for_illum.npy', for_illum)
        np.save(self.solver.system.path + 'rtm/adj_illum.npy', adj_illum)


    def plot_results(self, image):
        ''' Plot results during FWI iterations
        '''

        # calculate the scale
        scale = np.max(np.abs(image)) * 0.2

        # plot model
        plot_model(self.solver.model.x, 
            self.solver.model.z,
            image.reshape(self.solver.model.nx, self.solver.model.nz).T, 
            -scale, 
             scale,
            os.path.join(self.solver.system.path, 'rtm/image.png'), 
            'RTM', 
            figaspect = 1, 
            colormap = 'gray')


    def print_end_info(self, start_time):
        ''' Print end information
        '''
        # print the end information
        hours, rem = divmod(time.time()-start_time, 3600)
        minutes, seconds = divmod(rem, 60)
        print('RTM Workflow: finished in {:0>2}h {:0>2}m {:.0f}s\n'.format(int(hours), int(minutes), seconds))


class Configuration(object):
    ''' Configuration class converts dictionary to object attributes
    '''
    def __init__(self, dict):
        for _, value in dict.items():
            for k, v in value.items():
                setattr(self, k, v)



