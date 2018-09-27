import os

from scan_sequences.scans import NonTargetSequence
from natsort import natsorted
from nipype.interfaces.elastix import Registration, ApplyWarp
from utils import io_utils
import scipy.ndimage as sni
import numpy as np
import file_constants as fc
from utils import quant_vals as qv
import warnings


__EXPECTED_NUM_SPIN_LOCK_TIMES__ = 4
__R_SQUARED_THRESHOLD__ = 0.9
__INITIAL_P0_VALS__ = (1.0, 1/30.0)

__T1_RHO_LOWER_BOUND__ = 0
__T1_RHO_UPPER_BOUND__ = np.inf
__T1_RHO_DECIMAL_PRECISION__ = 1


class CubeQuant(NonTargetSequence):
    NAME = 'cube_quant'

    def __init__(self, dicom_path=None, dicom_ext=None, load_path=None):
        super().__init__(dicom_path, dicom_ext)

        self.t1rho_map = None
        self.subvolumes = None

        if load_path:
            self.load_data(load_path)

        if dicom_path is not None:
            self.subvolumes = self.__split_volumes__(__EXPECTED_NUM_SPIN_LOCK_TIMES__)
            self.intraregistered_data = self.__intraregister__(self.subvolumes)

        if self.subvolumes is None:
            raise ValueError('Either dicom_path or interregistered_volumes_path must be specified')

    def interregister(self, target_path, mask_path=None):
        base_spin_lock_time, base_image = self.intraregistered_data['BASE']
        files = self.intraregistered_data['FILES']

        temp_interregistered_dirpath = io_utils.check_dir(os.path.join(self.temp_path, 'interregistered'))

        print('')
        print('==' * 40)
        print('Interregistering...')
        print('Target: %s' % target_path)
        if mask_path is not None:
            print('Mask: %s' % mask_path)
        print('==' * 40)

        # get number of slices
        volume_shape = self.subvolumes[base_spin_lock_time].shape

        # Register base image to the target image
        print('Registering %s (base image)' % base_image)
        reg = Registration()
        reg.inputs.fixed_image = target_path
        reg.inputs.moving_image = base_image
        reg.inputs.output_path = io_utils.check_dir(os.path.join(temp_interregistered_dirpath,
                                                                 '%03d' % base_spin_lock_time))
        reg.inputs.parameters = [fc.ELASTIX_RIGID_PARAMS_FILE, fc.ELASTIX_AFFINE_PARAMS_FILE]

        if mask_path is not None:
            #raise ValueError('Moving mask not supported')

            mask, mask_spacing = io_utils.load_nifti(mask_path)

            fixed_mask = np.asarray(sni.gaussian_filter(np.asarray(mask, dtype=np.float32), sigma=3.0) > 0.2, dtype=np.int8)
            fixed_mask_filepath = os.path.join(temp_interregistered_dirpath, 'dilated-mask.nii.gz')
            io_utils.save_nifti(fixed_mask_filepath, fixed_mask, mask_spacing)
            reg.inputs.fixed_mask = fixed_mask_filepath

        #reg.terminal_output = 'none'
        reg_output = reg.run()
        reg_output = reg_output.outputs
        transformation_files = reg_output.transform
        warped_files = [(base_spin_lock_time, reg_output.warped_file)]

        # Load the transformation file. Apply same transform to the remaining images
        for spin_lock_time, filename in files:
            print('Applying transform %s' % filename)
            warped_file = ''
            for f in transformation_files:
                reg = ApplyWarp()
                reg.inputs.moving_image = filename

                reg.inputs.transform_file = f
                reg.inputs.output_path = io_utils.check_dir(os.path.join(temp_interregistered_dirpath,
                                                                  '%03d' % spin_lock_time))
                #reg.terminal_output = 'none'
                reg_output = reg.run()
                warped_file = reg_output.outputs.warped_file

            assert warped_file != ''

            # append the last warped file - this has all the transforms applied
            warped_files.append((spin_lock_time, warped_file))

        # copy each of the interregistered warped files to their own output
        subvolumes = dict()
        for spin_lock_time, warped_file in warped_files:
            subvolumes[spin_lock_time], _ = io_utils.load_nifti(warped_file)

        #self.subvolumes = self.__load_interregistered_files__(interregistered_dirpath)
        self.subvolumes = subvolumes


    def generate_t1_rho_map(self):
        svs = []
        spin_lock_times = []
        original_shape = None
        for spin_lock_time in self.subvolumes.keys():
            spin_lock_times.append(spin_lock_time)
            sv = self.subvolumes[spin_lock_time]

            if original_shape is None:
                original_shape = sv.shape
            else:
                assert(sv.shape == original_shape)

            svs.append(sv.reshape((1, -1)))

        vals = np.zeros(svs[0].shape)
        r_squared = np.zeros(svs[0].shape)
        svs = np.concatenate(svs)

        warned_negative = False
        for i in range(vals.shape[1]):
            if (svs[..., i] < 0).any() and not warned_negative:
                warned_negative = True
                warnings.warn("Negative values found. Failure in monoexponential fit will result in t1_rho=np.nan")

            try:
                params, r2 = qv.fit_mono_exp(spin_lock_times, svs[..., i], p0=__INITIAL_P0_VALS__)
                t1_rho = params[-1]
            except RuntimeError:
                t1_rho, r2 = (np.nan, 0.0)

            vals[..., i] = t1_rho
            r_squared[..., i] = r2

        map_unfiltered = vals.reshape(original_shape)
        r_squared = r_squared.reshape(original_shape)

        t1rho_map = map_unfiltered * (r_squared > __R_SQUARED_THRESHOLD__)

        # Filter calculated T1-rho values that are below 0ms and over 100ms
        t1rho_map[t1rho_map <= __T1_RHO_LOWER_BOUND__] = np.nan
        t1rho_map[t1rho_map > __T1_RHO_UPPER_BOUND__] = np.nan
        t1rho_map[np.isinf(t1rho_map)] = np.nan

        t1rho_map = np.around(t1rho_map, __T1_RHO_DECIMAL_PRECISION__)

        self.t1rho_map = t1rho_map

        return t1rho_map

    def __intraregister__(self, subvolumes):
        """
        Register subvolumes to each other using affine registration with elastix
        :param subvolumes:
        :return:
        """
        if subvolumes is None:
            raise ValueError('subvolumes must be dict()')

        print('')
        print('==' * 40)
        print('Intraregistering...')
        print('==' * 40)

        # temporarily save subvolumes as nifti file
        ordered_spin_lock_times = natsorted(list(subvolumes.keys()))
        raw_volumes_base_path = io_utils.check_dir(os.path.join(self.temp_path, 'raw'))

        # Use first spin lock time as a basis for registration
        spin_lock_nii_files = []
        for spin_lock_time in ordered_spin_lock_times:
            filepath = os.path.join(raw_volumes_base_path, '%03d' % spin_lock_time + '.nii.gz')
            spin_lock_nii_files.append(filepath)

            io_utils.save_nifti(filepath, subvolumes[spin_lock_time], self.pixel_spacing)

        target_filepath = spin_lock_nii_files[0]

        intraregistered_files = []
        for i in range(1,len(spin_lock_nii_files)):
            spin_file = spin_lock_nii_files[i]
            spin_lock_time = ordered_spin_lock_times[i]

            reg = Registration()
            reg.inputs.fixed_image = target_filepath
            reg.inputs.moving_image = spin_file
            reg.inputs.output_path = io_utils.check_dir(os.path.join(self.temp_path,
                                                                     'intraregistered',
                                                                     '%03d' % spin_lock_time))
            reg.inputs.parameters = [fc.ELASTIX_AFFINE_PARAMS_FILE]
            #reg.terminal_output = 'none'
            print('Registering %s --> %s' % (str(spin_lock_time), str(ordered_spin_lock_times[0])))
            tmp = reg.run()

            warped_file = tmp.outputs.warped_file
            intraregistered_files.append((spin_lock_time, warped_file))

        return {'BASE': (ordered_spin_lock_times[0], spin_lock_nii_files[0]),
                'FILES': intraregistered_files}

    def save_data(self, save_dirpath):
        save_dirpath = self.__save_dir__(save_dirpath)

        if self.t1rho_map:
            data = {'data': self.t1rho_map}
            io_utils.save_h5(os.path.join(save_dirpath, '%s.h5' % qv.QuantitativeValue.T1_RHO.name.lower()), data)

        # Save interregistered files
        interregistered_dirpath = os.path.join(save_dirpath, 'interregistered')

        for spin_lock_time in self.subvolumes.keys():
            filepath = os.path.join(interregistered_dirpath, '%03d.nii.gz' % spin_lock_time)
            io_utils.save_nifti(filepath, self.subvolumes[spin_lock_time], self.pixel_spacing)

    def load_data(self, load_dirpath):
        super().load_data(load_dirpath)
        load_dirpath = self.__save_dir__(load_dirpath, create_dir=False)

        interregistered_dirpath = os.path.join(load_dirpath, 'interregistered')

        self.subvolumes = self.__load_interregistered_files__(interregistered_dirpath)
