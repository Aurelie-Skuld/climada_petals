import logging
import numpy as np
import skimage
import copy
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)


@dataclass
class FilteringOrder:

    def __init__(self, thresholds, expand, operations, sizes):
        self.thresholds = thresholds
        self.expand = expand
        if len(operations) != len(sizes):
            LOGGER.warning('For every operation a filter size is needed and the other way around. '
                           'Please input the same number of operations and filter sizes.')
        self.operations = operations
        self.sizes = sizes


class Warn:
    """Explanation of defaults (also maybe transfer to config, talk to Emanuel)"""
    operations = ['DILATION', 'EROSION', 'DILATION', 'MEDIANFILTERING']
    sizes = [2, 3, 7, 15]

    def __init__(self, filter_data, data):
        self.filter_data = filter_data
        self.nr_thresholds = len(self.filter_data.thresholds) - 1
        self.warning = np.zeros_like(data)

    @classmethod
    def from_np_array(cls, data, thresholds, expand=True, operations=operations, sizes=sizes):  # pass defaults
        filter_data = FilteringOrder(thresholds, expand, operations, sizes)
        warn = cls(filter_data, data)
        data_thrs = warn.threshold_data(data)
        warn.filter_algorithm(data_thrs)
        return warn

    def threshold_data(self, data):
        if np.max(data) > np.max(self.filter_data.thresholds) or np.min(data) < np.min(self.filter_data.thresholds):
            LOGGER.warning('Values of data array are smaller/larger than defined thresholds. '
                           'Please redefine thresholds.')
        m_thrs = np.zeros_like(data)
        for i in range(1, len(self.filter_data.thresholds)):
            m_thrs[data > self.filter_data.thresholds[i]] = i
        return m_thrs.astype(int)

    def filter_algorithm(self, d_thrs):
        def filtering(d, warn_reg, curr_lvl):
            if self.filter_data.expand:  # select points where level is >= level under observation -> expands regions
                pts_curr_lvl = np.bitwise_or(warn_reg, d >= curr_lvl)
            else:  # select points where level is == level under observation -> not expanding regions
                pts_curr_lvl = d == curr_lvl
            reg_curr_lvl = np.where(pts_curr_lvl, curr_lvl, 0)  # set everything but pts of current level to 0

            for i in range(len(self.filter_data.operations)):
                if self.filter_data.operations[i] == 'DILATION':
                    reg_curr_lvl = skimage.morphology.dilation(reg_curr_lvl,
                                                               skimage.morphology.disk(self.filter_data.sizes[i]))
                elif self.filter_data.operations[i] == 'EROSION':
                    reg_curr_lvl = skimage.morphology.erosion(reg_curr_lvl,
                                                              skimage.morphology.disk(self.filter_data.sizes[i]))
                elif self.filter_data.operations[i] == 'MEDIANFILTERING':
                    filter_med = np.ones((self.filter_data.sizes[i], self.filter_data.sizes[i]))
                    reg_curr_lvl = skimage.filters.median(reg_curr_lvl, filter_med)
                else:
                    LOGGER.warning("The operation is not defined. "
                                   "Please select 'EROSION', 'DILATION', or 'MEDIANFILTERING'.")
            return reg_curr_lvl

        max_warn_level = np.max(d_thrs)
        if max_warn_level == 0:
            return np.zeros(d_thrs.shape)

        warn_regions = 0
        cnt = 0
        # iterate from highest level to lowest (0 not necessary, because rest is level 0)
        for j in range(max_warn_level, 0, -1):
            w_l = filtering(d_thrs, warn_regions, j)
            # keep warn regions of higher levels by taking maximum
            warn_regions = np.maximum(warn_regions, w_l)
            cnt += 4
        self.warning = warn_regions

    @staticmethod
    def remove_small_regions(warning, size_thr):

        def increase_levels(warn, size):
            # increase levels of too small regions to max level of this warning
            labels = skimage.measure.label(warn)
            for i in range(np.max(labels)):
                cnt = np.count_nonzero(labels == i)
                if cnt <= size:
                    warn[labels == i] = np.max(warn, axis=(0, 1))
            return warn

        def set_new_lvl(warn, size):
            # correct the max_lvl regions generated before down,
            # until the new regions with it are large enough
            for i in range(np.max(warn, axis=(0, 1)), np.min(warn, axis=(0, 1)), -1):
                level = copy.deepcopy(warn)
                level[warn != i] = 0
                labels = skimage.measure.label(warn)
                for j in range(len(np.unique(labels)) + 1):
                    cnt = np.count_nonzero(labels == j)
                    if cnt <= size:
                        warn[labels == j] = i - 1

        warning = warning + 1  # 0 is regarded as background in labelling, + 1 prevents this
        increase_levels(warning, size_thr)
        set_new_lvl(warning, size_thr)
        warning = warning - 1
        return warning
