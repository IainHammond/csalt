import os, sys, time, importlib
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from csalt.data import *
from csalt.data_mcfost import fitdata as fitdata_mcfost
from csalt.models import *
from priors import *
import emcee
import corner
from scipy import stats
import scipy.constants as sc
from multiprocessing import Pool
from schwimmbad import MPIPool


# log-posterior calculator
def lnprob(theta, code='default'):

    # compute the log-prior and return if problematic
    lnT = np.sum(logprior(theta)) * data_['nobs']
    if lnT == -np.inf:
        return -np.inf, -np.inf

    # loop through observations to compute the log-likelihood
    lnL = 0
    mcube = None
    for EB in range(data_['nobs']):

        # get the inference dataset
        dat = data_[str(EB)]

        # calculate model visibilities
        if EB == 0 or mcube is None:
            mvis, mcube = vismodel_iter(theta, fixed_, dat,
                             data_['gcf'+str(EB)], data_['corr'+str(EB)], code=code_)
        else:
            mvis, mcube = vismodel_iter(theta, fixed_, dat,
                             data_['gcf'+str(EB)], data_['corr'+str(EB)], code=code_,
                             mcube=mcube)

        # spectrally bin the model
        wt = dat.iwgt.reshape((dat.npol, -1, dat.chbin, dat.nvis))
        mvis_b = np.average(mvis.reshape((dat.npol, -1, dat.chbin,
                                          dat.nvis)), weights=wt, axis=2)


        # compute the residuals (stack both pols)
        resid = np.hstack(np.absolute(dat.vis - mvis_b))
        var = np.hstack(dat.wgt)

        # compute the log-likelihood
        lnL += -0.5 * np.tensordot(resid, np.dot(dat.inv_cov, var * resid))

    # return the log-posterior and log-prior
    return lnL + dat.lnL0 + lnT, lnT



# log-posterior calculator
def lnprob_naif(theta):

    # compute the log-prior and return if problematic
    lnT = np.sum(logprior(theta)) * data_['nobs']
    if lnT == -np.inf:
        return -np.inf, -np.inf

    # loop through observations to compute the log-likelihood
    lnL = 0
    for EB in range(data_['nobs']):

        # get the inference dataset
        dat = data_[str(EB)]

        # calculate model visibilities
        mvis = vismodel_naif(theta, fixed_, dat,
                             data_['gcf'+str(EB)], data_['corr'+str(EB)])

        # compute the residuals (stack both pols)
        resid = np.hstack(np.absolute(dat.vis - mvis))
        var = np.hstack(dat.wgt)

        # compute the log-likelihood
        lnL += -0.5 * np.tensordot(resid, np.dot(dat.inv_cov, var * resid))

    # return the log-posterior and log-prior
    return lnL + dat.lnL0 + lnT, lnT


def lnprob_naif_wdoppcorr(theta):

    # compute the log-prior and return if problematic
    lnT = np.sum(logprior(theta)) * data_['nobs']
    if lnT == -np.inf:
        return -np.inf, -np.inf

    # loop through observations to compute the log-likelihood
    lnL = 0
    for EB in range(data_['nobs']):

        # get the inference dataset
        dat = data_[str(EB)]

        # calculate model visibilities
        mvis = vismodel_naif_wdoppcorr(theta, fixed_, dat,
                                       data_['gcf'+str(EB)],
                                       data_['corr'+str(EB)])

        # compute the residuals (stack both pols)
        resid = np.hstack(np.absolute(dat.vis - mvis))
        var = np.hstack(dat.wgt)

        # compute the log-likelihood
        lnL += -0.5 * np.tensordot(resid, np.dot(dat.inv_cov, var * resid))

    # return the log-posterior and log-prior
    return lnL + dat.lnL0 + lnT, lnT


def run_sampler(mypool, mode, nwalk, ndim, steps, pos, backend):
    if mode == 'naif':
        print('\n Note: running in naif mode... \n')
        with mypool as pool:
            sampler = emcee.EnsembleSampler(nwalk, ndim, lnprob_naif,
                                            pool=pool, backend=backend)
            t0 = time.time()
            sampler.run_mcmc(pos, steps, progress=True)
        t1 = time.time()
    elif mode == 'naif_wdoppcorr':
        print('\n Note: running in naif mode with doppler correction... \n')
        with mypool as pool:
            sampler = emcee.EnsembleSampler(nwalk, ndim,
                                            lnprob_naif_wdoppcorr,
                                            pool=pool, backend=backend)
            t0 = time.time()
            sampler.run_mcmc(pos, steps, progress=True)
        t1 = time.time()
    else:
        with mypool as pool:
            sampler = emcee.EnsembleSampler(nwalk, ndim, lnprob, pool=pool,
                                            backend=backend)
            t0 = time.time()
            sampler.run_mcmc(pos, steps, progress=True)
        t1 = time.time()

    return sampler, t0, t1


def run_emcee(datafile, fixed, code=None, vra=None, vcensor=None,
              nwalk=75, ninits=200, nsteps=1000, chbin=3,
              outfile='stdout.h5', append=False, mode='iter', nthreads=6,
              mpi=False, theta=None):

    global code_
    code_ = code

    # load the data
    global data_
    print('Reading in data from h5 file')
    if code=='MCFOST':
        data_ = fitdata_mcfost(datafile, vra=vra, vcensor=vcensor,
                    nu_rest=fixed[0], chbin=chbin)
    else:
        data_ = fitdata(datafile, vra=vra, vcensor=vcensor,
                    nu_rest=fixed[0], chbin=chbin)

    print('Data read in from h5 file')

    # assign fixed
    global fixed_
    fixed_ = fixed

    # initialize parameters using random draws from the priors
    ndim = len(pri_pars)
    p0 = init_priors(ndim, nwalk)

    print('Priors initialised')

    data_ = build_cache(p0, data_, fixed_, code=code, mode=mode)

    print('Preliminary models run')

    # this is to see the log probability of a specific model and cancels run
    if theta is not None:
        log_post, log_pri = lnprob(theta)
        print(log_post, log_pri)
        return log_post, log_pri


    # Configure backend for recording posterior samples
    post_file = outfile
    if not append:
        # run to initialize
        if mpi:
            pool = MPIPool()
        else:
            pool = Pool(processes=nthreads)
        isampler, t0, t1 = run_sampler(pool, mode, nwalk, ndim, ninits, p0, backend=None)
        pool.close()

        print('Backend configured in %.2f hours' % ((t1 - t0) / 3600))

        # reset initialization to more compact distributions
        # this does random, uniform draws from the inner quartiles of the
        # walker distributions at the end initialization step (effectively
	# pruning outlier walkers stuck far from the mode)
        isamples = isampler.get_chain()	  # [ninits, nwalk, ndim]-shaped
        lop0 = np.quantile(isamples[-1, :, :], 0.25, axis=0)
        hip0 = np.quantile(isamples[-1, :, :], 0.75, axis=0)
        p00 = [np.random.uniform(lop0, hip0, ndim) for iw in range(nwalk)]
        print('\nChains now properly initialized...\n')

        # prepare the backend file for the full run
        os.system('rm -rf '+post_file)
        backend = emcee.backends.HDFBackend(post_file)
        backend.reset(nwalk, ndim)

        # run the MCMC
        if mpi:
            pool = MPIPool()
        else:
            pool = Pool(processes=nthreads)
        sampler, t0, t1 = run_sampler(pool, mode, nwalk, ndim, nsteps, p00, backend)
        pool.close()

    else:
        new_backend = emcee.backends.HDFBackend(post_file)
        print("Initial size: {0}".format(new_backend.iteration))

        if mpi:
            pool = MPIPool()
        else:
            pool = Pool(processes=nthreads)
        mew_sampler, t0, t1 = run_sampler(pool, mode, nwalk, ndim, nsteps-new_backend.iteration, pos=None, backend=new_backend)
        pool.close()
        print("Final size: {0}".format(new_backend.iteration))

    print(' ')
    print(' ')
    print('This run took %.2f hours' % ((t1 - t0) / 3600))

    return

def post_summary(p, prec=0.1, mu='peak', CIlevs=[84.135, 15.865, 50.]):

    # calculate percentiles as designated
    CI_p = np.percentile(p, CIlevs)

    # find peak of posterior
    if (mu == 'peak'):
        kde_p = stats.gaussian_kde(p)
        ndisc = np.int(np.round((CI_p[0] - CI_p[1]) / prec))
        x_p = np.linspace(CI_p[1], CI_p[0], ndisc)
        pk_p = x_p[np.argmax(kde_p.evaluate(x_p))]
    else:
        pk_p = np.percentile(p, 50.)

    # return the peak and upper, lower 1-sigma
    return (pk_p, CI_p[0]-pk_p, pk_p-CI_p[1], CI_p[2])



def post_analysis(outfile, burnin=0, autocorr=False, Ntau=200,
                  corner_plot=True, truths=None):

    # load the emcee backend file
    reader = emcee.backends.HDFBackend(outfile)

    # parse the samples
    all_samples = reader.get_chain(discard=0, flat=False)
    samples = reader.get_chain(discard=burnin, flat=False)
    samples_ = reader.get_chain(discard=burnin, flat=True)
    logpost_samples = reader.get_log_prob(discard=burnin, flat=False)
    logprior_samples = reader.get_blobs(discard=burnin, flat=False)
    nsteps, nwalk, ndim = samples.shape

    # set parameter labels, truths (NOT HARDCODE!)
    lbls = ['incl', 'PA', 'M', 'r_l', 'z0', 'psi', 'Tb0', 'q', 'Tback', 'dV0',
            'tau0', 'p', 'vsys', 'dx', 'dy']


    # Plot the integrated autocorrelation time every Ntau steps
    if autocorr:
        Nmax = all_samples.shape[0]
        if (Nmax > Ntau):
            tau_ix = np.empty(np.int(Nmax / Ntau))
            ix = np.empty(np.int(Nmax / Ntau))
            for i in range(len(tau_ix)):
                nn = (i + 1) * Ntau
                ix[i] = nn
                tau = emcee.autocorr.integrated_time(all_samples[:nn,:,:],
                                                     tol=0)
                tau_ix[i] = np.mean(tau)

        fig = plt.figure()
        plt.plot(ix, tau_ix, '-o')
        plt.xlabel('steps')
        plt.ylabel('autocorr time (steps)')
        plt.xlim([0, Nmax])
        plt.ylim([0, tau_ix.max() + 0.1 * (tau_ix.max() - tau_ix.min())])
        fig.savefig('autocorr.png')
        fig.clf()


    # Plot the traces
    fig = plt.figure(figsize=(15, 6))
    gs = gridspec.GridSpec(3, 6)

    # log-likelihood
    ax = fig.add_subplot(gs[0,0])
    for iw in range(nwalk):
        ax.plot(np.arange(nsteps),
                logpost_samples[:,iw] - logprior_samples[:,iw],
                color='k', alpha=0.03)
    ax.set_xlim([0, nsteps])
    ax.tick_params(which='both', labelsize=6)
    ax.set_ylabel('log likelihood', fontsize=6)
    ax.set_xticklabels([])
    ax.text(0.95, 0.05, 'ln L', fontsize=12, ha='right', color='purple',
            transform=ax.transAxes)

    # log-prior
    ax = fig.add_subplot(gs[0,1])
    for iw in range(nwalk):
        ax.plot(np.arange(nsteps), logprior_samples[:, iw],
                color='k', alpha=0.03)
    ax.set_xlim([0, nsteps])
    ax.set_ylim([np.min(logprior_samples[:, iw]) - 0.05,
                 np.max(logprior_samples[:, iw]) + 0.05])
    ax.tick_params(which='both', labelsize=6)
    ax.set_ylabel('log prior', fontsize=6)
    ax.set_xticklabels([])
    ax.text(0.95, 0.05, 'ln prior', fontsize=12, ha='right', color='purple',
            transform=ax.transAxes)

    # now cycle through parameters
    ax_ixl = [np.floor_divide(idim, 6) for idim in np.arange(2, ndim+2)]
    ax_ixh = [(idim % 6) for idim in np.arange(2, ndim+2)]
    for idim in range(ndim):
        ax = fig.add_subplot(gs[ax_ixl[idim], ax_ixh[idim]])
        for iw in range(nwalk):
            ax.plot(np.arange(nsteps), samples[:, iw, idim],
                    color='k', alpha=0.03)
        if truths is not None:
            ax.plot([0, nsteps], [truths[idim], truths[idim]], '--C1', lw=1.5)
        ax.set_xlim([0, nsteps])
        ax.tick_params(which='both', labelsize=6)
        ax.set_ylabel(lbls[idim], fontsize=6)
        if idim != 10:
            ax.set_xticklabels([])
        else:
            ax.set_xlabel('steps', fontsize=6)
        ax.text(0.95, 0.05, lbls[idim], fontsize=12, ha='right', color='purple',
                transform=ax.transAxes)

    fig.subplots_adjust(wspace=0.20, hspace=0.05)
    fig.subplots_adjust(left=0.03, right=0.97, bottom=0.05, top=0.99)
    fig.savefig('traces.png')
    fig.clf()


    # corner plot
    if corner_plot:
        levs = 1. - np.exp(-0.5 * (np.arange(3) + 1)**2)
        flat_chain = samples.reshape(-1, ndim)
        fig = corner.corner(flat_chain, plot_datapoints=False, levels=levs,
                            labels=lbls, truths=truths)
        fig.savefig('corner.png')
        fig.clf()


    # Parameter inferences (1-D marginalized)
    print(' ')
    prec = [0.01, 0.01, 0.001, 0.1, 0.0001, 0.01, 0.1, 0.001, 0.1, 0.1, 0.01,
            0.01, 0.1, 0.0001, 0.0001]
    for idim in range(ndim):
        fmt = '{:.'+str(np.abs(np.int(np.log10(prec[idim]))))+'f}'
        pk, hi, lo, med = post_summary(samples_[:,idim], prec=prec[idim])
        print((lbls[idim] + ' = '+fmt+' +'+fmt+' / -'+fmt).format(pk, hi, lo))
    print(' ')


def build_cache(p0, data_, fixed_, code, mode=None):

    mcube = None
    for EB in range(data_['nobs']):

        # initial model calculations
        if mode == 'naif':
            _mvis, gcf, corr = vismodel_naif(p0[0], fixed_, data_[str(EB)],
                                             return_holders=True)
        elif mode == 'naif_wdoppcorr':
            _mvis, gcf, corr = vismodel_naif_wdoppcorr(p0[0], fixed_,
                                                       data_[str(EB)],
                                                       return_holders=True)
        else:
            if EB==0 or mcube is not None:
                _mvis, gcf, corr, mcube = vismodel_def(p0[0], fixed_, data_[str(EB)],
                                            mtype=code, return_holders=True, mcube=mcube)
            else:
                _mvis, gcf, corr, mcube = vismodel_def(p0[0], fixed_, data_[str(EB)],
                                            mtype=code, return_holders=True)

        # add gcf, corr caches into data dictionary, indexed by EB
        data_['gcf'+str(EB)] = gcf
        data_['corr'+str(EB)] = corr

    return data_

def init_priors(ndim, nwalk):
    p0 = np.empty((nwalk, ndim))
    for ix in range(ndim):
        if pri_types[ix] == "normal" or pri_types[ix] == "uniform":
            _ = [str(pri_pars[ix][ip])+', ' for ip in range(len(pri_pars[ix]))]
            cmd = 'np.random.'+pri_types[ix]+'('+"".join(_)+str(nwalk)+')'
            p0[:,ix] = eval(cmd)
        elif pri_types[ix] == "truncnorm" or pri_types[ix] == "loguniform":
            if pri_types[ix] == "truncnorm":
                params = pri_pars[ix]
                mod_pri_pars = [(params[2]-params[0])/params[1], (params[3]-params[0])/params[1], params[0], params[1]]
                _ = [str(mod_pri_pars[ip])+', ' for ip in range(len(mod_pri_pars))]
            else:
                _ = [str(pri_pars[ix][ip])+', ' for ip in range(len(pri_pars[ix]))]
            cmd = 'stats.'+pri_types[ix]+'.rvs('+"".join(_)+'size='+str(nwalk)+')'
            p0[:,ix] = eval(cmd)
        else:
            raise NameError('Prior type unaccounted for')
    return p0
