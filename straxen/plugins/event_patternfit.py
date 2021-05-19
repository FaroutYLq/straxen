import strax
import straxen

import numpy as np
from scipy import special
from scipy.special import betainc, gammaln
from scipy.stats import binom_test, binom

import warnings

export, __all__ = strax.exporter()    

@export
@strax.takes_config(
    strax.Option('s1_optical_map', help="S1 (x, y, z) optical/pattern map.",
                 default="XENONnT_s1_xyz_patterns_LCE_corrected_qes_MCva43fa9b_wires.pkl"),
    strax.Option('s2_optical_map', help="S2 (x, y) optical/pattern map.",
                 default="XENONnT_s2_xy_patterns_LCE_corrected_qes_MCva43fa9b_wires.pkl"),
    strax.Option('s1_aft_map', help="Date drive S1 area fraction top map.",
                 default='s1_aft_dd_xyz_XENONnT_Kr83m_41500eV_12May2021.json'),
    strax.Option('MeanPEperPhoton', help="Mean of full VUV single photon responce",
                 default=1.2),
    strax.Option('gain_model',
                 help='PMT gain model. Specify as (model_type, model_config)'),
    strax.Option('n_tpc_pmts', type=int,
                 help='Number of TPC PMTs'),
    strax.Option('n_top_pmts', type=int,
                 help='Number of top TPC PMTs'),
    strax.Option('s1_min_reconstruction_area',
                 help='Skip reconstruction if S1 area (PE) is less than this'),
    strax.Option('s2_min_reconstruction_area',
                 help='Skip reconstruction if S2 area (PE) is less than this'),
    strax.Option('StorePerChannel', default=False, type=bool,
                 help="Store normalized LLH per channel for each peak"),
)
class EventPatternFit(strax.Plugin):
    """
    Plugin that provides patter information for events
    """
    depends_on = ('event_area_per_channel','event_info')
    provides = "event_patternfit"
    __version__="0.0.3"
   
    def infer_dtype(self):
        dtype = [('s2_2llh', np.float32,
                  f'Modified Poisson likelihood value for main S2 in the event'),
                 ('alt_s2_2llh', np.float32,
                  f'Modified Poisson likelihood value for alternative S2 in the event'),
                 ('s1_2llh', np.float32,
                  f'Modified Poisson likelihood value for main S1 in the event'),
                 ('s1_top_2llh', np.float32,
                  f'Modified Poisson likelihood value for main S1, observed from the top array, in the event'),
                 ('s1_bottom_2llh', np.float32,
                  f'Modified Poisson likelihood value for main S1, observed from the bottom array, in the event'),
                 ('s1_area_fraction_top_continuos_probability', np.float32, 
                  'Continuos binomial test for S1 area fraction top'),
                 ('s1_area_fraction_top_discrete_probability', np.float32, 
                  'Discrete binomial test for S1 area fraction top'),
                 ('s1_photon_fraction_top_continuos_probability', np.float32, 
                  'Continuos binomial test for S1 photon fraction top'),
                 ('s1_photon_fraction_top_discrete_probability', np.float32, 
                  'Discrete binomial test for S1 photon fraction top')]  
        
        if self.config['StorePerChannel']:
            dtype.append( (("LLH per channel for main S2","s2_2llh_per_channel"), 
                       np.float32, (self.config['n_top_pmts'],)) )
            dtype.append( (("LLH per channel for alternative S2","alt_s2_2llh_per_channel"), 
                       np.float32, (self.config['n_top_pmts'],)) )
            dtype.append( (("Pattern main S2","s2_pattern"), 
                       np.float32, (self.config['n_top_pmts'],)) )
            dtype.append( (("Pattern alt S2","alt_s2_pattern"), 
                       np.float32, (self.config['n_top_pmts'],)) )
            
        dtype += strax.time_fields
        return dtype
    
    def setup(self):
        self.mean_sPhoton = self.config['MeanPEperPhoton']
        
        ## Getting S1 AFT maps
        self.s1_aft_map = straxen.InterpolatingMap(straxen.get_resource(self.config['s1_aft_map'], fmt='json'))
                    
        ## Getting optical maps
        self.s1_pattern_map = straxen.InterpolatingMap(straxen.get_resource(self.config['s1_optical_map'], fmt='pkl'))
        self.s2_pattern_map = straxen.InterpolatingMap(straxen.get_resource(self.config['s2_optical_map'], fmt='pkl'))
        
        ## Getting gain model to get dead PMTs
        self.to_pe          = straxen.get_to_pe(self.run_id, self.config['gain_model'], self.config['n_tpc_pmts'])
        self.dead_PMTs      = np.where(self.to_pe==0)[0]
        self.pmtbool        = ~np.in1d(np.arange(0,self.config['n_tpc_pmts']), self.dead_PMTs)
        self.pmtbool_top    = self.pmtbool[:self.config['n_top_pmts']]
        self.pmtbool_bottom = self.pmtbool[self.config['n_top_pmts']:]

        
    def compute(self, events):
        
        result = np.zeros(len(events), dtype=self.dtype)
        result['time'], result['endtime'] = events['time'], strax.endtime(events)

        ## Computing LLH values for S1s
        self.compute_s1_llhvalue(events, result)
        
        ## Computing LLH values for S2s
        self.compute_s2_llhvalue(events, result)
        
        ## Computing binomial test for s1 area fraction top
        positions = np.vstack([events['x'], events['y'], events['z']]).T
        aft_prob  = self.s1_aft_map(positions)
        
        result['s1_area_fraction_top_continuos_probability'] = [
            s1_area_fraction_top_probability(_aft, _s1, _s1_aft) if not np.isnan(_aft*_s1*_s1_aft) 
            else np.nan
            for _aft, _s1, _s1_aft in zip(aft_prob, events['s1_area'], events['s1_area_fraction_top'])
        ]
        
        result['s1_area_fraction_top_discrete_probability']  = [
            s1_area_fraction_top_probability(_aft, _s1, _s1_aft, mode='discrete') if not np.isnan(_aft*_s1*_s1_aft)
            else np.nan
            for _aft, _s1, _s1_aft in zip(aft_prob, events['s1_area'], events['s1_area_fraction_top'])
        ]
        
        result['s1_photon_fraction_top_continuos_probability'] = [
            s1_area_fraction_top_probability(_aft, _s1/self.config['MeanPEperPhoton'], _s1_aft) if not np.isnan(_aft*_s1*_s1_aft) 
            else np.nan
            for _aft, _s1, _s1_aft in zip(aft_prob, events['s1_area'], events['s1_area_fraction_top'])
        ]
        
        result['s1_photon_fraction_top_discrete_probability']  = [
            s1_area_fraction_top_probability(_aft, _s1/self.config['MeanPEperPhoton'], _s1_aft, mode='discrete') if not np.isnan(_aft*_s1*_s1_aft)
            else np.nan
            for _aft, _s1, _s1_aft in zip(aft_prob, events['s1_area'], events['s1_area_fraction_top'])
        ]
        
        return(result)
    
    def compute_s1_llhvalue(self, events, result):
        result['s1_2llh'][:]        = 0.0
        result['s1_top_2llh'][:]    = 0.0
        result['s1_bottom_2llh'][:] = 0.0

        ### Selecting S1s for pattern fit calculation
        # - must exist (index != -1)
        # - must have total area larger minimal one
        # - must have positive AFT
        x, y, z      = events['x'], events['y'], events['z']
        cur_s1_bool  = events['s1_area']>self.config['s1_min_reconstruction_area']
        cur_s1_bool &= events['s1_index']!=-1
        cur_s1_bool &= events['s1_area_fraction_top']>=0

        ### Making expectation patterns [ in PE ]
        s1_map_effs = self.s1_pattern_map(np.array([x,y,z]).T)[cur_s1_bool,:]
        s1_area     = events['s1_area'][cur_s1_bool]
        s1_pattern  = s1_area[:,None]*(s1_map_effs[:,self.pmtbool])/np.sum(s1_map_effs[:,self.pmtbool], axis=1)[:,None] 
        _s1_pattern_top     = (events['s1_area_fraction_top'][cur_s1_bool]*s1_area)
        s1_pattern_top      = _s1_pattern_top[:,None]*((s1_map_effs[:,:self.config['n_top_pmts']])[:,self.pmtbool_top])
        s1_pattern_top     /= np.sum((s1_map_effs[:,:self.config['n_top_pmts']])[:,self.pmtbool_top], axis=1)[:,None] 
        _s1_pattern_bottom  = ((1-events['s1_area_fraction_top'][cur_s1_bool])*s1_area)
        s1_pattern_bottom   = _s1_pattern_bottom[:,None]*((s1_map_effs[:,self.config['n_top_pmts']:])[:,self.pmtbool_bottom])
        s1_pattern_bottom  /= np.sum((s1_map_effs[:,self.config['n_top_pmts']:])[:,self.pmtbool_bottom], axis=1)[:,None] 

        ## Getting pattern from data
        s1_area_per_channel_ = events['s1_area_per_channel'][cur_s1_bool,:]
        s1_area_per_channel  = s1_area_per_channel_[:, self.pmtbool]
        s1_area_per_channel_top    = (s1_area_per_channel_[:, :self.config['n_top_pmts']])[:, self.pmtbool_top]
        s1_area_per_channel_bottom = (s1_area_per_channel_[:, self.config['n_top_pmts']:])[:, self.pmtbool_bottom]

        # Top and bottom
        arg1         = s1_pattern/self.mean_sPhoton,          s1_area_per_channel, self.mean_sPhoton
        arg2         = s1_area_per_channel/self.mean_sPhoton, s1_area_per_channel, self.mean_sPhoton
        norm_llh_val = (neg2llh_modpoisson(*arg1) - neg2llh_modpoisson(*arg2))
        result['s1_2llh'][cur_s1_bool] = np.sum(norm_llh_val, axis=1)

        # Top
        arg1         = s1_pattern_top/self.mean_sPhoton,          s1_area_per_channel_top, self.mean_sPhoton
        arg2         = s1_area_per_channel_top/self.mean_sPhoton, s1_area_per_channel_top, self.mean_sPhoton
        norm_llh_val = (neg2llh_modpoisson(*arg1) - neg2llh_modpoisson(*arg2))
        result['s1_top_2llh'][cur_s1_bool] = np.sum(norm_llh_val, axis=1)

        # Bottom
        arg1         = s1_pattern_bottom/self.mean_sPhoton,          s1_area_per_channel_bottom, self.mean_sPhoton
        arg2         = s1_area_per_channel_bottom/self.mean_sPhoton, s1_area_per_channel_bottom, self.mean_sPhoton
        norm_llh_val = (neg2llh_modpoisson(*arg1) - neg2llh_modpoisson(*arg2))
        result['s1_bottom_2llh'][cur_s1_bool] = np.sum(norm_llh_val, axis=1)

                
    def compute_s2_llhvalue(self, events,result):
        for t_ in ['s2', 'alt_s2']:
            result[t_+'_2llh'][:]=0.0

            ### Selecting S2s for pattern fit calculation
            # - must exist (index != -1)
            # - must have total area larger minimal one
            # - must have positive AFT
            x,y = events[t_+'_x'], events[t_+'_y']
            cur_s2_bool  = (events[t_+'_area']>self.config['s2_min_reconstruction_area'])
            cur_s2_bool *= (events[t_+'_index']!=-1)
            cur_s2_bool *= (events["s2_area_fraction_top"]>0)
            
            ### Making expectation patterns [ in PE ]
            s2_map_effs = self.s2_pattern_map(np.array([x,y]).T)[cur_s2_bool,0:self.config['n_top_pmts']]
            s2_map_effs = s2_map_effs[:,self.pmtbool_top]
            s2_top_area = (events["s2_area_fraction_top"]*events["s2_area"])[cur_s2_bool]        
            s2_pattern  = s2_top_area[:,None]*s2_map_effs/np.sum(s2_map_effs,axis=1)[:,None]  
            
            ## Getting pattern from data
            s2_top_area_per_channel = events['s2_area_per_channel'][cur_s2_bool,0:self.config['n_top_pmts']]
            s2_top_area_per_channel = s2_top_area_per_channel[:,self.pmtbool_top]
            
            ## Calculating LLH, this is shifted Poisson
            # we get area expectation and we need to scale them to get
            # photon expectation
            norm_llh_val = (neg2llh_modpoisson(
                                 mu    = s2_pattern/self.mean_sPhoton, 
                                 areas = s2_top_area_per_channel, 
                                 mean_sPhoton=self.mean_sPhoton)
                                    - 
                            neg2llh_modpoisson(
                                 mu    = s2_top_area_per_channel/self.mean_sPhoton, 
                                 areas = s2_top_area_per_channel, 
                                 mean_sPhoton=self.mean_sPhoton)
                           )
            result[t_+'_2llh'][cur_s2_bool]=np.sum(norm_llh_val,axis=1)
            if self.config['StorePerChannel']:
                result[t_+'_pattern'][:]=0.0
                result[t_+'_2llh_per_channel'][:]=0.0
                store_patterns = np.zeros((s2_pattern.shape[0],self.config['n_top_pmts']) )
                store_patterns[:,self.pmtbool_top]=s2_pattern
                result[t_+'_pattern'][cur_s2_bool]=store_patterns#:s2_pattern[cur_s2_bool]
                ### 
                store_2LLH_ch = np.zeros((norm_llh_val.shape[0],self.config['n_top_pmts']) )
                store_2LLH_ch[:,self.pmtbool_top]=norm_llh_val
                result[t_+'_2llh_per_channel'][cur_s2_bool]=store_2LLH_ch
            ####
            
            
def neg2llh_modpoisson(mu=None, areas=None, mean_sPhoton=1.0):
    """ 
    Modified poisson distribution with proper normalization for shifted poisson. 
    
    mu - expected number of photons per channel
    areas  - observed areas per channel
    mean_sPhoton - mean 
    """
    res          = 2.*(mu - (areas/mean_sPhoton)*np.log(mu) + special.loggamma((areas/mean_sPhoton)+1) + np.log(mean_sPhoton)) 
    is_zero      = ~(areas>0)    # If area equals or smaller than 0 - assume 0
    res[is_zero] = 2.*mu[is_zero]
    # if zero channel has negative expectation, assume LLH to be 0 there
    # this happens for normalization factor
    neg_mu              = mu<0.0 
    res[is_zero*neg_mu] = 0.0 # 
    return(res)

# Continuos and discrete binomial test 
# https://github.com/poliastro/cephes/blob/master/src/bdtr.c

def bdtrc(k, n, p):
    if (k < 0):  return (1.0)
    if (k == n): return (0.0)
    dn = n - k
    if (k == 0):
        if (p < .01): dk = -np.expm1(dn * np.log1p(-p))
        else:         dk = 1.0 - np.exp(dn * np.log(1.0 - p))
    else:
        dk = k + 1
        dk = betainc(dk, dn, p)
    return dk
 
def bdtr(k, n, p):
    if (k < 0):  return np.nan
    if (k == n): return (1.0)
    dn = n - k
    if (k == 0): dk = np.exp(dn * np.log(1.0 - p))
    else:
        dk = k + 1
        dk = betainc(dn, dk, 1.0 - p)
    return dk

# Continuos binomial distribution
def binom_pmf(k, n, p):
    scale_log = gammaln(n + 1) - gammaln(n - k + 1) - gammaln(k + 1)
    ret_log = scale_log + k * np.log(p) + (n - k) * np.log(1 - p)
    return np.exp(ret_log)
 
def binom_cdf(k, n, p):
    return bdtr(k, n, p)
 
def binom_sf(k, n, p):
    return bdtrc(k, n, p)
 
def binom_test(k, n, p):
    '''
    The main purpose of this algorithm is to find the value j on the
    other side of the mean that has the same probability as k, and
    integrate the tails outward from k and j. In the case where either
    k or j are zero, only the non-zero tail is integrated.
    '''
    check = True
    if n < k:                 
        warnings.warn(f'n {n} must be >= k {k}')
        check = False
    if (p > 1.0) or (p < 0.0): 
        warnings.warn(f'p {p} must be in range [0, 1]')
        check = False
    if k < 0:                  
        warnings.warn(f'k {k} must be >= 0')
        check = False
        
    if check:
        d      = binom_pmf(k, n, p)
        rerr   = 1 + 1e-7
        d      = d * rerr
        n_iter = int(max(np.round(np.log10(n)) + 1, 2))   
        
        if k < n * p:
            if binom_pmf(n, n, p) > d:
                for n_ in np.arange(n, 2*n, 1):
                    if binom_pmf(n_, n, p) < d:
                        j_min, j_max = k, n_
                        do_test = True
                        break
                    do_test = False                  
            else: 
                j_min, j_max = k, n 
                do_test = True
            def check(d, y0, y1): return (d>y1)and(d<=y0)
            
        else:
            if binom_pmf(0, n, p) > d: 
                n_iter, j_min, j_max = 0, 0, 0
                do_test = True
            else:                      
                j_min, j_max = 0, k
                do_test = True
            def check(d, y0, y1): return (d>=y0)and(d<y1)
        
        # if B(k;n,p) is already 0 or I can't find the k' in the other side of the mean
        # the returned binomial test is 0
        if (d==0)|(not do_test):
            pval = 0
            return pval
        
        else:
            for i in range(n_iter):      
                n_pts   = int(j_max - j_min)
                j_range = np.linspace(j_min, j_max, n_pts, endpoint=True)
                y       = binom_pmf(j_range, n, p)
                for i in range(len(j_range) - 1):
                    if check(d, y[i], y[i + 1]):                    
                        j_min, j_max = j_range[i], j_range[i + 1]
                        break      
            j = max(min((j_min + j_max) / 2, n), 0)

            if k * j == 0:  pval = binom_sf(max(k, j), n, p)
            else:           pval = binom_cdf(min(k, j), n, p) + binom_sf(max(k, j), n, p)
            return min(1.0, pval)
    else:
        return np.nan
    
def s1_area_fraction_top_probability(aft_prob, area_tot, area_fraction_top, mode='continuos'):
    """
    Wrapper that does the S1 AFT probability calculation for you
    """
    size_top = area_tot * area_fraction_top
    size_tot = area_tot

    if mode == 'discrete': 
        return binom_pmf(size_top, size_tot, aft_prob)
    else:                  
        return binom_test(size_top, size_tot, aft_prob)
