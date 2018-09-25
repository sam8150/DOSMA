import os

from scan_sequences.scans import NonTargetSequence
from natsort import natsorted
from nipype.interfaces.elastix import Registration, ApplyWarp
from utils import io_utils
import scipy.ndimage as sni
import numpy as np
import file_constants as fc
from utils import quant_vals as qv
import shutil

__EXPECTED_NUM_SPIN_LOCK_TIMES__ = 4
__R_SQUARED_THRESHOLD__ = 0.9
__INITIAL_P0_VALS__ = (1.0, 30.0)

__T1_RHO_LOWER_BOUND__ = 0
__T1_RHO_UPPER_BOUND__ = np.inf
__T1_RHO_DECIMAL_PRECISION__ = 1

class CubeQuant(NonTargetSequence):
    NAME = 'cube_quant'

    def __init__(self, dicom_path=None, dicom_ext=None, save_dir=None, interregistered_volumes_path=''):
        super().__init__(dicom_path, dicom_ext)
        self.subvolumes = None
        self.t1rho_map = None

        if dicom_path is not None and not interregistered_volumes_path:
            self.save_dir = save_dir

            self.subvolumes = self.__split_volumes__(__EXPECTED_NUM_SPIN_LOCK_TIMES__)
            self.intermediate_save_dir = os.path.join(self.save_dir, self.NAME)
            self.intraregistered_data = self.__intraregister__(self.subvolumes)
        elif interregistered_volumes_path:
            print('')
            print('%s - Loading interregistered volumes' % self.NAME)
            print('Path: %s' % interregistered_volumes_path)
            self.subvolumes = self.__load_interregistered_files__(interregistered_volumes_path)

        if self.subvolumes is None:
            raise ValueError('Either dicom_path or interregistered_volumes_path must be specified')

    def interregister(self, target_path, mask_path=None):
        base_spin_lock_time, base_image = self.intraregistered_data['BASE']
        files = self.intraregistered_data['FILES']

        interregistered_dirpath = io_utils.check_dir(os.path.join(self.intermediate_save_dir, 'interregistered'))
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
            mask = io_utils.load_nifti(mask_path)
            mask_shape = mask.shape
            mask = sni.zoom(mask, (volume_shape[0] / mask_shape[0],
                                   volume_shape[1] / mask_shape[1],
                                   volume_shape[2] / mask_shape[2]))

            assert mask.shape == volume_shape, 'Shape mismatch - mask: %s, volume: %s' % (str(mask.shape), str(volume_shape))
            moving_mask = np.asarray(sni.gaussian_filter(np.asarray(mask, dtype=np.float32), sigma=3.0) > 0.5, dtype=np.int8)
            moving_mask_filepath = os.path.join(temp_interregistered_dirpath, 'dilated-mask.nii.gz')
            io_utils.save_nifti(moving_mask_filepath, moving_mask)
            reg.inputs.moving_mask = moving_mask_filepath

        reg.terminal_output = 'none'
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
                reg.terminal_output = 'none'
                reg_output = reg.run()
                warped_file = reg_output.outputs.warped_file

            assert warped_file != ''

            # append the last warped file - this has all the transforms applied
            warped_files.append((spin_lock_time, warped_file))

        # copy each of the interregistered warped files to their own output

        for spin_lock_time, warped_file in warped_files:
            shutil.copyfile(warped_file, os.path.join(interregistered_dirpath, '%s.nii.gz' % str(spin_lock_time)))

        self.subvolumes = self.__load_interregistered_files__(interregistered_dirpath)

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

        for i in range(len(vals.shape[1])):
            vals[i], r_squared[i] = qv.fit_mono_exp(spin_lock_times, svs[..., i], p0=__INITIAL_P0_VALS__)

        map_unfiltered = vals.reshape(original_shape)
        r_squared = r_squared.reshape(original_shape)

        t1rho_map = map_unfiltered * (r_squared > __R_SQUARED_THRESHOLD__)
        # Filter calculated T2 values that are below 0ms and over 100ms
        t1rho_map[t1rho_map < __T1_RHO_LOWER_BOUND__] = 0.0
        t1rho_map[t1rho_map > __T1_RHO_UPPER_BOUND__] = 0.0
        t1rho_map[np.isnan(t1rho_map)] = 0.0
        t1rho_map[np.isinf(t1rho_map)] = 0.0

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
        raw_volumes_base_path = os.path.join(self.temp_path, 'raw')

        # Use first spin lock time as a basis for registration
        spin_lock_nii_files = []
        count = 1
        for spin_lock_time in ordered_spin_lock_times:
            filepath = os.path.join(raw_volumes_base_path, '%03d' % count + '.nii.gz')
            spin_lock_nii_files.append(filepath)

            io_utils.save_nifti(filepath, subvolumes[spin_lock_time])

            count += 1

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
            reg.terminal_output = 'none'
            print('Registering %s --> %s' % (str(spin_lock_time), str(ordered_spin_lock_times[0])))
            tmp = reg.run()

            warped_file = tmp.outputs.warped_file
            intraregistered_files.append((spin_lock_time, warped_file))

        return {'BASE': (ordered_spin_lock_times[0], spin_lock_nii_files[0]),
                'FILES': intraregistered_files}

    def save_data(self, save_dirpath):
        data = {qv.QuantitativeValue.T1_RHO.name: self.t1rho_map}
        io_utils.save_h5(os.path.join(save_dirpath, self.__data_filename__()), data)

    def load_data(self, load_dirpath):
        data = io_utils.load_h5(os.path.join(load_dirpath, self.__data_filename__()))
        self.t1rho_map = data[qv.QuantitativeValue.T1_RHO.name]




