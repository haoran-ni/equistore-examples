import numpy as np
import torch

import skcosmo.sample_selection

from equistore import Labels, TensorBlock, TensorMap

from .utils import structure_sum, dot, power, normalize, detach
import utils.models.operations as ops


def _select_support_points_for_block(block: TensorBlock, n_select: int):
    assert len(block.components) == 0

    fps = skcosmo.sample_selection.FPS(n_to_select=n_select)

    array = block.values
    if isinstance(array, torch.Tensor):
        array = array.detach()
    fps.fit_transform(array)

    return TensorBlock(
        values=array[fps.selected_idx_],
        samples=block.samples[fps.selected_idx_],
        components=block.components,
        properties=block.properties,
    )


def select_support_points(ps, n_select):
    if isinstance(n_select, int):
        block = _select_support_points_for_block(ps.block(), n_select)

        return TensorMap(Labels.single(), [block])

    else:
        blocks = []
        for key, value in n_select.items():
            block = _select_support_points_for_block(
                ps.block(**key.as_dict()),
                n_select=value,
            )
            blocks.append(block)

        return TensorMap(ps.keys, blocks)


class SparseKernelGap:
    def __init__(self, support_points, zeta, regularizer, jitter=1e-13):
        self.zeta = zeta
        self.jitter = jitter
        self.regularizer = regularizer
        self.support_points = normalize(detach(support_points))

        self.weights = None
        self.baseline = None

    def fit(self, ps, energies, forces=None):
        k_mm = self._compute_kernel(self.support_points)
        K_MM = []
        for _, k_mm_block in k_mm:
            assert len(k_mm_block.components) == 0
            K_MM.append(k_mm_block.values)

        K_MM = ops.block_diag(*K_MM)
        K_MM[np.diag_indices_from(K_MM)] += self.jitter

        k_nm = self._compute_kernel(ps)
        k_nm = structure_sum(k_nm, sum_properties=False)

        # TODO: make sure this create an array in the same order as
        # `scipy.linalg.block_diag` above
        if len(k_nm.keys.names) != 0:
            names = list(k_nm.keys.names)
            k_nm.keys_to_properties(names)

        k_nm = k_nm.block()
        assert len(k_nm.components) == 0
        K_NM = k_nm.values

        self.baseline = energies.mean()

        delta = energies.std()
        structures = np.unique(k_nm.samples["structure"])
        n_atoms_per_structure = []
        for structure in structures:
            n_atoms = np.sum(k_nm.samples["structure"] == structure)
            n_atoms_per_structure.append(float(n_atoms))

        energy_regularizer = (
            ops.sqrt(ops.array_like(energies, n_atoms_per_structure))
            * self.regularizer[0]
            / delta
        )

        K_NM[:] /= energy_regularizer[:, None]

        Y = (energies.reshape(-1, 1) - self.baseline) / energy_regularizer[:, None]

        if forces is not None:
            k_nm_gradient = k_nm.gradient("positions")
            k_nm_grad = k_nm_gradient.data.reshape(
                3 * k_nm_gradient.data.shape[0], k_nm_gradient.data.shape[2]
            )

            forces_regularizer = self.regularizer[1] / delta
            k_nm_grad[:] /= forces_regularizer

            energy_grad = -forces.reshape(-1, 1)
            energy_grad[:] /= forces_regularizer

            Y = ops.vstack([Y, energy_grad])
            K_NM = ops.vstack([K_NM, k_nm_grad])

        K = K_MM + K_NM.T @ K_NM
        Y = K_NM.T @ Y

        self.weights = ops.linalg_solve(K, Y, detach=True)

    def predict(self, ps, with_forces=False):
        if self.weights is None:
            raise Exception("call fit first")

        k_per_atom = self._compute_kernel(ps)
        k_per_structure = structure_sum(k_per_atom, sum_properties=False)
        # TODO: make sure this create an array in the same order as block_diag above
        if len(k_per_structure.keys.names) != 0:
            names = list(k_per_structure.keys.names)
            k_per_structure.keys_to_properties(names)

        assert len(k_per_structure.block().components) == 0
        kernel = k_per_structure.block().values

        energies = kernel @ self.weights + self.baseline

        if with_forces:
            kernel_gradient = k_per_structure.block().gradient("positions")

            forces = -kernel_gradient.data @ self.weights
            forces = forces.reshape(-1, 3)
        else:
            forces = None

        return energies, forces

    def _compute_kernel(self, ps):
        return power(dot(ps, self.support_points, do_normalize=True), zeta=self.zeta)
