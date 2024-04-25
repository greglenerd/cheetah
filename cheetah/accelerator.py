from abc import ABC, abstractmethod
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, Optional, Union

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import torch
from matplotlib.patches import Rectangle
from scipy import constants
from scipy.constants import physical_constants
from torch import nn
from torch.distributions import MultivariateNormal

from cheetah.converters.dontbmad import convert_bmad_lattice
from cheetah.converters.nxtables import read_nx_tables
from cheetah.latticejson import load_cheetah_model, save_cheetah_model
from cheetah.particles import Beam, ParameterBeam, ParticleBeam
from cheetah.track_methods import base_rmatrix, misalignment_matrix, rotation_matrix
from cheetah.utils import UniqueNameGenerator

c = torch.tensor(constants.speed_of_light)
generate_unique_name = UniqueNameGenerator(prefix="unnamed_element")
elementary_charge= torch.tensor(constants.elementary_charge)
rest_energy = torch.tensor(
    constants.electron_mass
    * constants.speed_of_light**2
    / constants.elementary_charge  # electron mass
)
electron_mass_eV = torch.tensor(
    physical_constants["electron mass energy equivalent in MeV"][0] * 1e6
)
electron_mass = torch.tensor(
    physical_constants["electron mass"][0]
)

epsilon_0 = torch.tensor(constants.epsilon_0)

class Element(ABC, nn.Module):
    """
    Base class for elements of particle accelerators.

    :param name: Unique identifier of the element.
    """

    def __init__(self, name: Optional[str] = None) -> None:
        super().__init__()

        self.name = name if name is not None else generate_unique_name()

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        """
        Generates the element's transfer map that describes how the beam and its
        particles are transformed when traveling through the element.
        The state vector consists of 6 values with a physical meaning:
        (in the trace space notation)

        - x: Position in x direction
        - xp: Angle in x direction
        - y: Position in y direction
        - yp: Angle in y direction
        - s: Position in longitudinal direction, the zero value is set to the
        reference position (usually the center of the pulse)
        - p: Relative energy deviation from the reference particle
           :math:`p = \frac{\Delta E}{p_0 C}`
        As well as a seventh value used to add constants to some of the prior values if
        necessary. Through this seventh state, the addition of constants can be
        represented using a matrix multiplication.

        :param energy: Reference energy of the Beam. Read from the fed-in Cheetah Beam.
        :return: A 7x7 Matrix for further calculations.
        """
        raise NotImplementedError

    def track(self, incoming: Beam) -> Beam:
        """
        Track particles through the element. The input can be a `ParameterBeam` or a
        `ParticleBeam`.

        :param incoming: Beam of particles entering the element.
        :return: Beam of particles exiting the element.
        """
        if incoming is Beam.empty:
            return incoming
        elif isinstance(incoming, ParameterBeam):
            tm = self.transfer_map(incoming.energy)
            mu = torch.matmul(tm, incoming._mu)
            cov = torch.matmul(tm, torch.matmul(incoming._cov, tm.t()))
            return ParameterBeam(
                mu,
                cov,
                incoming.energy,
                total_charge=incoming.total_charge,
                device=mu.device,
                dtype=mu.dtype,
            )
        elif isinstance(incoming, ParticleBeam):
            tm = self.transfer_map(incoming.energy)
            new_particles = torch.matmul(incoming.particles, tm.t())
            return ParticleBeam(
                new_particles,
                incoming.energy,
                particle_charges=incoming.particle_charges,
                device=new_particles.device,
                dtype=new_particles.dtype,
            )
        else:
            raise TypeError(f"Parameter incoming is of invalid type {type(incoming)}")

    def forward(self, incoming: Beam) -> Beam:
        """Forward function required by `torch.nn.Module`. Simply calls `track`."""
        return self.track(incoming)

    @property
    @abstractmethod
    def is_skippable(self) -> bool:
        """
        Whether the element can be skipped during tracking. If `True`, the element's
        transfer map is combined with the transfer maps of surrounding skipable
        elements.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def defining_features(self) -> list[str]:
        """
        List of features that define the element. Used to compare elements for equality
        and to save them.

        NOTE: When overriding this property, make sure to call the super method and
        extend the list it returns.
        """
        return []

    @abstractmethod
    def split(self, resolution: torch.Tensor) -> list["Element"]:
        """
        Split the element into slices no longer than `resolution`. Some elements may not
        be splittable, in which case a list containing only the element itself is
        returned.

        :param resolution: Length of the longest allowed split in meters.
        :return: Ordered sequence of sliced elements.
        """
        raise NotImplementedError

    @abstractmethod
    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        """
        Plot a representation of this element into a `matplotlib` Axes at position `s`.

        :param ax: Axes to plot the representation into.
        :param s: Position of the object along s in meters.
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={repr(self.name)})"


class CustomTransferMap(Element):
    """
    This element can represent any custom transfer map.
    """

    def __init__(
        self,
        transfer_map: Union[torch.Tensor, nn.Parameter],
        length: Optional[torch.Tensor] = None,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        assert isinstance(transfer_map, torch.Tensor)
        assert transfer_map.shape == (7, 7)

        self._transfer_map = torch.as_tensor(transfer_map, **factory_kwargs)
        self.length = (
            torch.as_tensor(length, **factory_kwargs)
            if length is not None
            else torch.tensor(0.0, **factory_kwargs)
        )

    @classmethod
    def from_merging_elements(
        cls, elements: list[Element], incoming_beam: Beam
    ) -> "CustomTransferMap":
        """
        Combine the transfer maps of multiple successive elements into a single transfer
        map. This can be used to speed up tracking through a segment, if no changes
        are made to the elements in the segment or the energy of the beam being tracked
        through them.

        :param elements: List of consecutive elements to combine.
        :param incoming_beam: Beam entering the first element in the segment. NOTE: That
            this is required because the separate original transfer maps have to be
            computed before being combined and some of them may depend on the energy of
            the beam.
        """
        assert all(element.is_skippable for element in elements), (
            "Combining the elements in a Segment that is not skippable will result in"
            " incorrect tracking results."
        )

        device = elements[0].transfer_map(incoming_beam.energy).device
        dtype = elements[0].transfer_map(incoming_beam.energy).dtype

        tm = torch.eye(7, device=device, dtype=dtype)
        for element in elements:
            tm = torch.matmul(element.transfer_map(incoming_beam.energy), tm)
            incoming_beam = element.track(incoming_beam)

        combined_length = sum(
            element.length for element in elements if hasattr(element, "length")
        )

        return cls(tm, length=combined_length, device=device, dtype=dtype)

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        return self._transfer_map

    @property
    def is_skippable(self) -> bool:
        return True

    def defining_features(self) -> list[str]:
        return super().defining_features + ["transfer_map"]

    def split(self, resolution: torch.Tensor) -> list[Element]:
        return [self]

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        # TODO: At some point think of a nice way to indicate this in a lattice plot
        pass


class Drift(Element):
    """
    Drift section in a particle accelerator.

    Note: the transfer map now uses the linear approximation.
    Including the R_56 = L / (beta**2 * gamma **2)

    :param length: Length in meters.
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        length: Union[torch.Tensor, nn.Parameter],
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.length = torch.as_tensor(length, **factory_kwargs)

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        device = self.length.device
        dtype = self.length.dtype

        gamma = energy / rest_energy.to(device=device, dtype=dtype)
        igamma2 = (
            1 / gamma**2
            if gamma != 0
            else torch.tensor(0.0, device=device, dtype=dtype)
        )
        beta = torch.sqrt(1 - igamma2)

        tm = torch.eye(7, device=device, dtype=dtype)
        tm[0, 1] = self.length
        tm[2, 3] = self.length
        tm[4, 5] = -self.length / beta**2 * igamma2

        return tm

    @property
    def is_skippable(self) -> bool:
        return True

    def split(self, resolution: torch.Tensor) -> list[Element]:
        split_elements = []
        remaining = self.length
        while remaining > 0:
            element = Drift(torch.min(resolution, remaining))
            split_elements.append(element)
            remaining -= resolution
        return split_elements

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        pass

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + ["length"]

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(length={repr(self.length)})"

class SpaceChargeKick(Element):
    """
    Simulates space charge effects on a beam.
    :param grid_precision: Number of grid points in each dimension.
    :param grid_dimensions: Dimensions of the grid as multiples of sigma of the beam.
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        length: Union[torch.Tensor, nn.Parameter],
        nx: Union[torch.Tensor, nn.Parameter,int]=32,
        ny: Union[torch.Tensor, nn.Parameter,int]=32,
        ns: Union[torch.Tensor, nn.Parameter,int]=32,
        dx: Union[torch.Tensor, nn.Parameter]=3,
        dy: Union[torch.Tensor, nn.Parameter]=3,
        ds: Union[torch.Tensor, nn.Parameter]=3,
        
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.length = torch.as_tensor(length, **factory_kwargs)
        self.nx = int(torch.as_tensor(nx, **factory_kwargs))
        self.ny = int(torch.as_tensor(ny, **factory_kwargs))
        self.ns = int(torch.as_tensor(ns, **factory_kwargs))
        self.dx = torch.as_tensor(dx, **factory_kwargs) # in multiples of sigma
        self.dy = torch.as_tensor(dy, **factory_kwargs)
        self.ds = torch.as_tensor(ds, **factory_kwargs)


    def grid_shape(self) -> tuple[int,int,int]:
        return (int(self.nx), int(self.ny), int(self.ns))  
    

    def grid_dimensions(self,beam: ParticleBeam) -> torch.Tensor:
        if beam.particles.shape[0] < 2:
            sigma_x = torch.tensor(175e-9)
            sigma_y = torch.tensor(175e-9)
            sigma_s = torch.tensor(175e-9)
        else:
            sigma_x = torch.std(beam.particles[:, 0])
            sigma_y = torch.std(beam.particles[:, 2])
            sigma_s = torch.std(beam.particles[:, 4])
        return torch.tensor([self.dx*sigma_x, self.dy*sigma_y, self.ds*sigma_s], device=self.dx.device)
    
    def delta_t(self,beam: ParticleBeam) -> torch.Tensor:
        return self.length / (c*self.betaref(beam))

    def cell_size(self,beam: ParticleBeam) -> torch.Tensor:
        grid_shape = self.grid_shape()
        grid_dimensions = self.grid_dimensions(beam)
        return 2*grid_dimensions / torch.tensor(grid_shape)
    
    def gammaref(self,beam: ParticleBeam) -> torch.Tensor:
        return beam.energy / rest_energy
    
    def betaref(self,beam: ParticleBeam) -> torch.Tensor:
        gamma = self.gammaref(beam)
        if gamma == 0:
            return torch.tensor(1.0)
        return torch.sqrt(1 - 1 / gamma**2)
    
    def space_charge_deposition(self, beam: ParticleBeam) -> torch.Tensor:
        """
        Deposition of the beam on the grid.
        """
        grid_shape = self.grid_shape()
        grid_dimensions = self.grid_dimensions(beam)
        cell_size = self.cell_size(beam)
        betaref = self.betaref(beam)

        # Initialize the charge density grid
        charge = torch.zeros(grid_shape, dtype=torch.float32)

        # Get particle positions and charges
        particle_pos = beam.particles[:, [0, 2, 4]]
        particle_pos[:,2] = -particle_pos[:,2]*betaref #modify with the tau transformation
        particle_charge = beam.particle_charges

        # Compute the normalized positions of the particles within the grid
        normalized_pos = (particle_pos + grid_dimensions) / cell_size

        # Find the indices of the lower corners of the cells containing the particles
        cell_indices = torch.floor(normalized_pos).type(torch.long)

        # Calculate the weights for all surrounding cells
        offsets = torch.tensor([[0, 0, 0], [0, 0, 1], [0, 1, 0], [0, 1, 1], [1, 0, 0], [1, 0, 1], [1, 1, 0], [1, 1, 1]])
        surrounding_indices = cell_indices.unsqueeze(1) + offsets  # Shape: (n_particles, 8, 3)
        weights = 1 - torch.abs(normalized_pos.unsqueeze(1) - surrounding_indices)  # Shape: (n_particles, 8, 3)
        cell_weights = weights.prod(dim=2)  # Shape: (n_particles, 8)

        # Add the charge contributions to the cells
        idx_x, idx_y, idx_s = surrounding_indices.view(-1, 3).T
        valid_mask = (idx_x >= 0) & (idx_x < grid_shape[0]) & \
                    (idx_y >= 0) & (idx_y < grid_shape[1]) & \
                    (idx_s >= 0) & (idx_s < grid_shape[2])

        # Accumulate the charge contributions
        indices = torch.stack([idx_x[valid_mask], idx_y[valid_mask], idx_s[valid_mask]], dim=0)
        repeated_charges = particle_charge.repeat_interleave(8)
        values = (cell_weights.view(-1) * repeated_charges)[valid_mask]
        charge.index_put_(tuple(indices), values, accumulate=True)
        cell_volume = cell_size[0] * cell_size[1] * cell_size[2]

        return charge/cell_volume  # Normalize by the cell volume, so that the charge density is in C/m^3
    

    def integrated_potential(self, x, y, s) -> torch.Tensor:
        r = torch.sqrt(x**2 + y**2 + s**2)
        G = (-0.5 * s**2 * torch.atan(x * y / (s * r))
            -0.5 * y**2 * torch.atan(x * s / (y * r))
            -0.5 * x**2 * torch.atan(y * s / (x * r))
            + y * s * torch.asinh(x / torch.sqrt(y**2 + s**2))
            + x * s * torch.asinh(y / torch.sqrt(x**2 + s**2))
            + x * y * torch.asinh(s / torch.sqrt(x**2 + y**2)))
        return G
    

    def cyclic_rho(self,beam: ParticleBeam) -> torch.Tensor:
        """
        Compute the charge density on the grid using the cyclic deposition method.
        """
        grid_shape = self.grid_shape()
        charge_density = self.space_charge_deposition(beam)

        # Double the dimensions
        new_dims = tuple(dim * 2 for dim in grid_shape)

        # Create a new tensor with the doubled dimensions, filled with zeros
        cyclic_charge_density = torch.zeros(new_dims)

        # Copy the original charge_density values to the beginning of the new tensor
        cyclic_charge_density[:charge_density.shape[0], :charge_density.shape[1], :charge_density.shape[2]] = charge_density
        return cyclic_charge_density    
    
    def IGF(self, beam: ParticleBeam) -> torch.Tensor:
        gamma = self.gammaref(beam)
        cell_size = self.cell_size(beam)
        dx, dy, ds = cell_size[0], cell_size[1], cell_size[2] * gamma  # ds is scaled by gamma
        nx, ny, ns = self.grid_shape()
        
        # Create coordinate grids
        x = torch.arange(nx) * dx
        y = torch.arange(ny) * dy
        s = torch.arange(ns) * ds
        x_grid, y_grid, s_grid = torch.meshgrid(x, y, s, indexing='ij')

        # Compute the Green's function values
        G_values = (
            self.integrated_potential(x_grid + 0.5 * dx, y_grid + 0.5 * dy, s_grid + 0.5 * ds)
            - self.integrated_potential(x_grid - 0.5 * dx, y_grid + 0.5 * dy, s_grid + 0.5 * ds)
            - self.integrated_potential(x_grid + 0.5 * dx, y_grid - 0.5 * dy, s_grid + 0.5 * ds)
            - self.integrated_potential(x_grid + 0.5 * dx, y_grid + 0.5 * dy, s_grid - 0.5 * ds)
            + self.integrated_potential(x_grid + 0.5 * dx, y_grid - 0.5 * dy, s_grid - 0.5 * ds)
            + self.integrated_potential(x_grid - 0.5 * dx, y_grid + 0.5 * dy, s_grid - 0.5 * ds)
            + self.integrated_potential(x_grid - 0.5 * dx, y_grid - 0.5 * dy, s_grid + 0.5 * ds)
            - self.integrated_potential(x_grid - 0.5 * dx, y_grid - 0.5 * dy, s_grid - 0.5 * ds)
        )

        # Initialize the grid with double dimensions
        grid = torch.zeros(2 * nx, 2 * ny, 2 * ns)

        # Fill the grid with G_values and its periodic copies
        grid[:nx, :ny, :ns] = G_values
        grid[nx+1:, :ny, :ns] = G_values[1:,:,:].flip(dims=[0]) # Reverse the x dimension, excluding the first element
        grid[:nx, ny+1:, :ns] = G_values[:, 1:,:].flip(dims=[1])  # Reverse the y dimension, excluding the first element
        grid[:nx, :ny, ns+1:] = G_values[:, :, 1:].flip(dims=[2]) # Reverse the s dimension, excluding the first element
        grid[nx+1:, ny+1:, :ns] = G_values[1:, 1:,:].flip(dims=[0, 1]) # Reverse the x and y dimensions
        grid[:nx, ny+1:, ns+1:] = G_values[:, 1:, 1:].flip(dims=[1, 2]) # Reverse the y and s dimensions
        grid[nx+1:, :ny, ns+1:] = G_values[1:, :, 1:].flip(dims=[0, 2])  # Reverse the x and s dimensions
        grid[nx+1:, ny+1:, ns+1:] = G_values[1:, 1:, 1:].flip(dims=[0, 1, 2])  # Reverse all dimensions

        return grid
    

    def solve_poisson_equation(self, beam: ParticleBeam) -> torch.Tensor:  #works only for ParticleBeam at this stage
        """
        Solves the Poisson equation for the given charge density.
        """
        charge_density = self.cyclic_rho(beam)
        charge_density_ft = torch.fft.fftn(charge_density)
        integrated_green_function = self.IGF(beam)
        integrated_green_function_ft = torch.fft.fftn(integrated_green_function)
        potential_ft = charge_density_ft * integrated_green_function_ft
        potential = (1/(4*torch.pi*epsilon_0))*torch.fft.ifftn(potential_ft).real

        # Return the physical potential
        return potential[:charge_density.shape[0]//2, :charge_density.shape[1]//2, :charge_density.shape[2]//2]


    def E_plus_vB_field(self, beam: ParticleBeam) -> torch.Tensor:
        """
        Compute the force field from the potential and the particle positions and speeds.
        """
        gamma = self.gammaref(beam)
        igamma2 = (
            1 / gamma**2
            if gamma != 0
            else torch.tensor(0.0)
        )
        potential = self.solve_poisson_equation(beam)
        cell_size = self.cell_size(beam)
        potential = potential.unsqueeze(0).unsqueeze(0)
        
        # Now apply padding
        phi_padded = torch.nn.functional.pad(potential, (1, 1, 1, 1, 1, 1), mode='replicate')
        phi_padded = phi_padded.squeeze(0).squeeze(0)
        # Compute derivatives using central differences
        grad_x = (phi_padded[2:, :, :] - phi_padded[:-2, :, :]) / (2 * cell_size[0])
        grad_y = (phi_padded[:, 2:, :] - phi_padded[:, :-2, :]) / (2 * cell_size[1])
        grad_z = (phi_padded[:, :, 2:] - phi_padded[:, :, :-2]) / (2 * cell_size[2])
        
        # Crop out the padding to maintain the original shape
        grad_x = -igamma2*grad_x[:, 1:-1, 1:-1]
        grad_y = -igamma2*grad_y[1:-1, :, 1:-1]
        grad_z = -igamma2*grad_z[1:-1, 1:-1, :]

        return grad_x, grad_y, grad_z

    def cheetah_to_moments(self, beam: ParticleBeam) -> torch.Tensor:
        N = beam.particles.shape[0]
        moments = beam.particles
        gammaref = self.gammaref(beam)
        betaref = self.betaref(beam)
        p0 = gammaref*betaref*electron_mass*c
        gamma = gammaref*(torch.ones(N)+beam.particles[:,5]*betaref)
        gamma = torch.clamp(gamma, min=1.0)
        beta = torch.sqrt(1 - 1 / gamma**2)
        p = gamma*electron_mass*beta*c
        moments[:,1] = p0*moments[:,1]
        moments[:,3] = p0*moments[:,3]
        moments[:,5] = torch.sqrt(p**2 - moments[:,1]**2 - moments[:,3]**2)
        return moments

    def moments_to_cheetah(self, moments: torch.Tensor, beam: ParticleBeam) -> torch.Tensor:
        N = moments.shape[0]
        gammaref = self.gammaref(beam)
        betaref = self.betaref(beam)
        p0 = gammaref*betaref*electron_mass*c
        p = torch.sqrt(moments[:,1]**2 + moments[:,3]**2 + moments[:,5]**2)
        gamma = torch.sqrt(1 + (p / (electron_mass*c))**2)
        moments[:,1] = moments[:,1] / p0
        moments[:,3] = moments[:,3] / p0
        moments[:,5] = (gamma-gammaref*torch.ones(N))/(betaref*gammaref)
        return moments

    def read_forces(self, beam: ParticleBeam) -> torch.Tensor:
        """
        Compute the momentum kick from the force field.
        """
        grad_x, grad_y, grad_z = self.E_plus_vB_field(beam)
        grid_shape = self.grid_shape()
        grid_dimensions = self.grid_dimensions(beam)
        cell_size = self.cell_size(beam)

        particle_pos = beam.particles[:, [0, 2, 4]] 
        particle_charge = beam.particle_charges

        normalized_pos = (particle_pos + grid_dimensions) / cell_size

        # Find the indices of the lower corners of the cells containing the particles
        cell_indices = torch.floor(normalized_pos).type(torch.long)

        # Calculate the weights for all surrounding cells
        offsets = torch.tensor([[0, 0, 0], [0, 0, 1], [0, 1, 0], [0, 1, 1], [1, 0, 0], [1, 0, 1], [1, 1, 0], [1, 1, 1]])
        surrounding_indices = cell_indices.unsqueeze(1) + offsets  # Shape: (n_particles, 8, 3)
        weights = 1 - torch.abs(normalized_pos.unsqueeze(1) - surrounding_indices)  # Shape: (n_particles, 8, 3)
        cell_weights = weights.prod(dim=2)  # Shape: (n_particles, 8)

        # Extract forces from the grids
        def get_force_values(force_grid):
            idx_x, idx_y, idx_s = surrounding_indices.view(-1, 3).T
            valid_mask = (idx_x >= 0) & (idx_x < grid_shape[0]) & \
                        (idx_y >= 0) & (idx_y < grid_shape[1]) & \
                        (idx_s >= 0) & (idx_s < grid_shape[2])
            
            valid_indices = torch.stack([idx_x[valid_mask], idx_y[valid_mask], idx_s[valid_mask]], dim=0)
            force_values = force_grid[tuple(valid_indices)]
            return force_values, valid_mask

        Fx_values, valid_mask_x = get_force_values(grad_x)
        Fy_values, valid_mask_y = get_force_values(grad_y)
        Fz_values, valid_mask_z = get_force_values(grad_z)

        # Compute interpolated forces
        interpolated_forces = torch.zeros((particle_pos.shape[0], 3), device=grad_x.device)
        values_x = cell_weights.view(-1)[valid_mask_x] * Fx_values * particle_charge.repeat_interleave(8)[valid_mask_x]
        values_y = cell_weights.view(-1)[valid_mask_y] * Fy_values * particle_charge.repeat_interleave(8)[valid_mask_y]
        values_z = cell_weights.view(-1)[valid_mask_z] * Fz_values * particle_charge.repeat_interleave(8)[valid_mask_z]
        
        indices = torch.arange(particle_pos.shape[0]).repeat_interleave(8)
        interpolated_forces.index_add_(0, indices[valid_mask_x], torch.stack([values_x, torch.zeros_like(values_x), torch.zeros_like(values_x)], dim=1))
        interpolated_forces.index_add_(0, indices[valid_mask_y], torch.stack([torch.zeros_like(values_y), values_y, torch.zeros_like(values_y)], dim=1))
        interpolated_forces.index_add_(0, indices[valid_mask_z], torch.stack([torch.zeros_like(values_z), torch.zeros_like(values_z), values_z], dim=1))

        return interpolated_forces
    

    def track(self, incoming: ParticleBeam) -> ParticleBeam:
        """
        Track particles through the element. The input must be a `ParticleBeam`.

        :param incoming: Beam of particles entering the element.
        :return: Beam of particles exiting the element.
        """
        if incoming is Beam.empty:
            return incoming
        elif isinstance(incoming, ParticleBeam):
            forces = self.read_forces(incoming)
            particles = self.cheetah_to_moments(incoming)
            particles[:,1] += forces[:,0]*self.delta_t(incoming)
            particles[:,3] += forces[:,1]*self.delta_t(incoming)
            particles[:,5] += forces[:,2]*self.delta_t(incoming)
            particles = self.moments_to_cheetah(particles, incoming)
            return ParticleBeam(
                particles,
                incoming.energy,
                particle_charges=incoming.particle_charges,
                device=particles.device,
                dtype=particles.dtype,
            )
        else:
            raise TypeError(f"Parameter incoming is of invalid type {type(incoming)}")


    def split(self, resolution: torch.Tensor) -> list[Element]:
    # TODO: Implement splitting for SpaceCharge properly, for now just returns the
    # element itself
        return [self]


    @property
    def is_skippable(self) -> bool:
        return True

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        pass

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + ["length"]

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(length={repr(self.length)})"


class Quadrupole(Element):
    """
    Quadrupole magnet in a particle accelerator.

    :param length: Length in meters.
    :param k1: Strength of the quadrupole in rad/m.
    :param misalignment: Misalignment vector of the quadrupole in x- and y-directions.
    :param tilt: Tilt angle of the quadrupole in x-y plane [rad]. pi/4 for
        skew-quadrupole.
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        length: Union[torch.Tensor, nn.Parameter],
        k1: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        misalignment: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        tilt: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.length = torch.as_tensor(length, **factory_kwargs)
        self.k1 = (
            torch.as_tensor(k1, **factory_kwargs)
            if k1 is not None
            else torch.tensor(0.0, **factory_kwargs)
        )
        self.misalignment = (
            torch.as_tensor(misalignment, **factory_kwargs)
            if misalignment is not None
            else torch.tensor([0.0, 0.0], **factory_kwargs)
        )
        self.tilt = (
            torch.as_tensor(tilt, **factory_kwargs)
            if tilt is not None
            else torch.tensor(0.0, **factory_kwargs)
        )

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        device = self.length.device
        dtype = self.length.dtype

        R = base_rmatrix(
            length=self.length,
            k1=self.k1,
            hx=torch.tensor(0.0, device=device, dtype=dtype),
            tilt=self.tilt,
            energy=energy,
        )

        if self.misalignment[0] == 0 and self.misalignment[1] == 0:
            return R
        else:
            R_exit, R_entry = misalignment_matrix(self.misalignment)
            R = torch.matmul(R_exit, torch.matmul(R, R_entry))
            return R

    @property
    def is_skippable(self) -> bool:
        return True

    @property
    def is_active(self) -> bool:
        return self.k1 != 0

    def split(self, resolution: torch.Tensor) -> list[Element]:
        split_elements = []
        remaining = self.length
        while remaining > 0:
            element = Quadrupole(
                torch.min(resolution, remaining),
                self.k1,
                misalignment=self.misalignment,
            )
            split_elements.append(element)
            remaining -= resolution
        return split_elements

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        alpha = 1 if self.is_active else 0.2
        height = 0.8 * (np.sign(self.k1) if self.is_active else 1)
        patch = Rectangle(
            (s, 0), self.length, height, color="tab:red", alpha=alpha, zorder=2
        )
        ax.add_patch(patch)

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + ["length", "k1", "misalignment", "tilt"]

    def __repr__(self) -> None:
        return (
            f"{self.__class__.__name__}(length={repr(self.length)}, "
            + f"k1={repr(self.k1)}, "
            + f"misalignment={repr(self.misalignment)}, "
            + f"tilt={repr(self.tilt)}, "
            + f"name={repr(self.name)})"
        )


class Dipole(Element):
    """
    Dipole magnet (by default a sector bending magnet).

    :param length: Length in meters.
    :param angle: Deflection angle in rad.
    :param e1: The angle of inclination of the entrance face [rad].
    :param e2: The angle of inclination of the exit face [rad].
    :param tilt: Tilt of the magnet in x-y plane [rad].
    :param fringe_integral: Fringe field integral (of the enterance face).
    :param fringe_integral_exit: (only set if different from `fint`) Fringe field
        integral of the exit face.
    :param gap: The magnet gap [m], NOTE in MAD and ELEGANT: HGAP = gap/2
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        length: Union[torch.Tensor, nn.Parameter],
        angle: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        e1: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        e2: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        tilt: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        fringe_integral: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        fringe_integral_exit: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        gap: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ):
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.length = torch.as_tensor(length, **factory_kwargs)
        self.angle = (
            torch.as_tensor(angle, **factory_kwargs)
            if angle is not None
            else torch.tensor(0.0, **factory_kwargs)
        )
        self.gap = (
            torch.as_tensor(gap, **factory_kwargs)
            if gap is not None
            else torch.tensor(0.0, **factory_kwargs)
        )
        self.tilt = (
            torch.as_tensor(tilt, **factory_kwargs)
            if tilt is not None
            else torch.tensor(0.0, **factory_kwargs)
        )
        self.name = name
        self.fringe_integral = (
            torch.as_tensor(fringe_integral, **factory_kwargs)
            if fringe_integral is not None
            else torch.tensor(0.0, **factory_kwargs)
        )
        self.fringe_integral_exit = (
            self.fringe_integral
            if fringe_integral_exit is None
            else torch.as_tensor(fringe_integral_exit, **factory_kwargs)
        )
        # Rectangular bend
        self.e1 = (
            torch.as_tensor(e1, **factory_kwargs)
            if e1 is not None
            else torch.tensor(0.0, **factory_kwargs)
        )
        self.e2 = (
            torch.as_tensor(e2, **factory_kwargs)
            if e2 is not None
            else torch.tensor(0.0, **factory_kwargs)
        )

    @property
    def hx(self) -> torch.Tensor:
        if self.length == 0.0:
            return torch.tensor(0.0, device=self.length.device, dtype=self.length.dtype)
        else:
            return self.angle / self.length

    @property
    def is_skippable(self) -> bool:
        return True

    @property
    def is_active(self):
        return self.angle != 0

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        device = self.length.device
        dtype = self.length.dtype

        R_enter = self._transfer_map_enter()
        R_exit = self._transfer_map_exit()

        if self.length != 0.0:  # Bending magnet with finite length
            R = base_rmatrix(
                length=self.length,
                k1=torch.tensor(0.0, device=device, dtype=dtype),
                hx=self.hx,
                tilt=torch.tensor(0.0, device=device, dtype=dtype),
                energy=energy,
            )
        else:  # Reduce to Thin-Corrector
            R = torch.eye(7, device=device, dtype=dtype)
            R[0, 1] = self.length
            R[2, 6] = self.angle
            R[2, 3] = self.length

        # Apply fringe fields
        R = torch.matmul(R_exit, torch.matmul(R, R_enter))
        # Apply rotation for tilted magnets
        R = torch.matmul(
            rotation_matrix(-self.tilt), torch.matmul(R, rotation_matrix(self.tilt))
        )

        return R

    def _transfer_map_enter(self) -> torch.Tensor:
        """Linear transfer map for the entrance face of the dipole magnet."""
        device = self.length.device
        dtype = self.length.dtype

        sec_e = torch.tensor(1.0, device=device, dtype=dtype) / torch.cos(self.e1)
        phi = (
            self.fringe_integral
            * self.hx
            * self.gap
            * sec_e
            * (1 + torch.sin(self.e1) ** 2)
        )

        tm = torch.eye(7, device=device, dtype=dtype)
        tm[1, 0] = self.hx * torch.tan(self.e1)
        tm[3, 2] = -self.hx * torch.tan(self.e1 - phi)

        return tm

    def _transfer_map_exit(self) -> torch.Tensor:
        """Linear transfer map for the exit face of the dipole magnet."""
        device = self.length.device
        dtype = self.length.dtype

        sec_e = 1.0 / torch.cos(self.e2)
        phi = (
            self.fringe_integral_exit
            * self.hx
            * self.gap
            * sec_e
            * (1 + torch.sin(self.e2) ** 2)
        )

        tm = torch.eye(7, device=device, dtype=dtype)
        tm[1, 0] = self.hx * torch.tan(self.e2)
        tm[3, 2] = -self.hx * torch.tan(self.e2 - phi)

        return tm

    def split(self, resolution: torch.Tensor) -> list[Element]:
        # TODO: Implement splitting for dipole properly, for now just returns the
        # element itself
        return [self]

    def __repr__(self):
        return (
            f"{self.__class__.__name__}(length={repr(self.length)}, "
            + f"angle={repr(self.angle)}, "
            + f"e1={repr(self.e1)},"
            + f"e2={repr(self.e2)},"
            + f"tilt={repr(self.tilt)},"
            + f"fringe_integral={repr(self.fringe_integral)},"
            + f"fringe_integral_exit={repr(self.fringe_integral_exit)},"
            + f"gap={repr(self.gap)},"
            + f"name={repr(self.name)})"
        )

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + [
            "length",
            "angle",
            "e1",
            "e2",
            "tilt",
            "fringe_integral",
            "fringe_integral_exit",
            "gap",
        ]

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        alpha = 1 if self.is_active else 0.2
        height = 0.8 * (np.sign(self.angle) if self.is_active else 1)

        patch = Rectangle(
            (s, 0), self.length, height, color="tab:green", alpha=alpha, zorder=2
        )
        ax.add_patch(patch)


class RBend(Dipole):
    """
    Rectangular bending magnet.

    :param length: Length in meters.
    :param angle: Deflection angle in rad.
    :param e1: The angle of inclination of the entrance face [rad].
    :param e2: The angle of inclination of the exit face [rad].
    :param tilt: Tilt of the magnet in x-y plane [rad].
    :param fringe_integral: Fringe field integral (of the enterance face).
    :param fringe_integral_exit: (only set if different from `fint`) Fringe field
        integral of the exit face.
    :param gap: The magnet gap [m], NOTE in MAD and ELEGANT: HGAP = gap/2
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        length: Optional[Union[torch.Tensor, nn.Parameter]],
        angle: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        e1: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        e2: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        tilt: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        fringe_integral: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        fringe_integral_exit: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        gap: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ):
        angle = angle if angle is not None else torch.tensor(0.0)
        e1 = e1 if e1 is not None else torch.tensor(0.0)
        e2 = e2 if e2 is not None else torch.tensor(0.0)
        tilt = tilt if tilt is not None else torch.tensor(0.0)
        fringe_integral = (
            fringe_integral if fringe_integral is not None else torch.tensor(0.0)
        )
        # fringe_integral_exit is left out on purpose
        gap = gap if gap is not None else torch.tensor(0.0)

        e1 = e1 + angle / 2
        e2 = e2 + angle / 2

        super().__init__(
            length=length,
            angle=angle,
            e1=e1,
            e2=e2,
            tilt=tilt,
            fringe_integral=fringe_integral,
            fringe_integral_exit=fringe_integral_exit,
            gap=gap,
            name=name,
            device=device,
            dtype=dtype,
        )


class HorizontalCorrector(Element):
    """
    Horizontal corrector magnet in a particle accelerator.
    Note: This is modeled as a drift section with
        a thin-kick in the horizontal plane.

    :param length: Length in meters.
    :param angle: Particle deflection angle in the horizontal plane in rad.
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        length: Union[torch.Tensor, nn.Parameter],
        angle: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.length = torch.as_tensor(length, **factory_kwargs)
        self.angle = (
            torch.as_tensor(angle, **factory_kwargs)
            if angle is not None
            else torch.tensor(0.0, **factory_kwargs)
        )

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        device = self.length.device
        dtype = self.length.dtype

        gamma = energy / rest_energy.to(device=device, dtype=dtype)
        igamma2 = (
            1 / gamma**2
            if gamma != 0
            else torch.tensor(0.0, device=device, dtype=dtype)
        )
        beta = torch.sqrt(1 - igamma2)

        tm = torch.eye(7, device=device, dtype=dtype)
        tm[0, 1] = self.length
        tm[1, 6] = self.angle
        tm[2, 3] = self.length
        tm[4, 5] = -self.length / beta**2 * igamma2

        return tm

    @property
    def is_skippable(self) -> bool:
        return True

    @property
    def is_active(self) -> bool:
        return self.angle != 0

    def split(self, resolution: torch.Tensor) -> list[Element]:
        split_elements = []
        remaining = self.length
        while remaining > 0:
            length = torch.min(resolution, remaining)
            element = HorizontalCorrector(length, self.angle * length / self.length)
            split_elements.append(element)
            remaining -= resolution
        return split_elements

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        alpha = 1 if self.is_active else 0.2
        height = 0.8 * (np.sign(self.angle) if self.is_active else 1)

        patch = Rectangle(
            (s, 0), self.length, height, color="tab:blue", alpha=alpha, zorder=2
        )
        ax.add_patch(patch)

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + ["length", "angle"]

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(length={repr(self.length)}, "
            + f"angle={repr(self.angle)}, "
            + f"name={repr(self.name)})"
        )


class VerticalCorrector(Element):
    """
    Verticle corrector magnet in a particle accelerator.
    Note: This is modeled as a drift section with
        a thin-kick in the vertical plane.

    :param length: Length in meters.
    :param angle: Particle deflection angle in the vertical plane in rad.
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        length: Union[torch.Tensor, nn.Parameter],
        angle: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.length = torch.as_tensor(length, **factory_kwargs)
        self.angle = (
            torch.as_tensor(angle, **factory_kwargs)
            if angle is not None
            else torch.tensor(0.0, **factory_kwargs)
        )

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        device = self.length.device
        dtype = self.length.dtype

        gamma = energy / rest_energy.to(device=device, dtype=dtype)
        igamma2 = (
            1 / gamma**2
            if gamma != 0
            else torch.tensor(0.0, device=device, dtype=dtype)
        )
        beta = torch.sqrt(1 - igamma2)

        tm = torch.eye(7, device=device, dtype=dtype)
        tm[0, 1] = self.length
        tm[2, 3] = self.length
        tm[3, 6] = self.angle
        tm[4, 5] = -self.length / beta**2 * igamma2
        return tm

    @property
    def is_skippable(self) -> bool:
        return True

    @property
    def is_active(self) -> bool:
        return self.angle != 0

    def split(self, resolution: torch.Tensor) -> list[Element]:
        split_elements = []
        remaining = self.length
        while remaining > 0:
            length = torch.min(resolution, remaining)
            element = VerticalCorrector(length, self.angle * length / self.length)
            split_elements.append(element)
            remaining -= resolution
        return split_elements

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        alpha = 1 if self.is_active else 0.2
        height = 0.8 * (np.sign(self.angle) if self.is_active else 1)

        patch = Rectangle(
            (s, 0), self.length, height, color="tab:cyan", alpha=alpha, zorder=2
        )
        ax.add_patch(patch)

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + ["length", "angle"]

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(length={repr(self.length)}, "
            + f"angle={repr(self.angle)}, "
            + f"name={repr(self.name)})"
        )


class Cavity(Element):
    """
    Accelerating cavity in a particle accelerator.

    :param length: Length in meters.
    :param voltage: Voltage of the cavity in volts.
    :param phase: Phase of the cavity in degrees.
    :param frequency: Frequency of the cavity in Hz.
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        length: Union[torch.Tensor, nn.Parameter],
        voltage: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        phase: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        frequency: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.length = torch.as_tensor(length, **factory_kwargs)
        self.voltage = (
            torch.as_tensor(voltage, **factory_kwargs)
            if voltage is not None
            else torch.tensor(0.0, **factory_kwargs)
        )
        self.phase = (
            torch.as_tensor(phase, **factory_kwargs)
            if phase is not None
            else torch.tensor(0.0, **factory_kwargs)
        )
        self.frequency = (
            torch.as_tensor(frequency, **factory_kwargs)
            if frequency is not None
            else torch.tensor(0.0, **factory_kwargs)
        )

    @property
    def is_active(self) -> bool:
        return self.voltage != 0

    @property
    def is_skippable(self) -> bool:
        return not self.is_active

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        device = self.length.device
        dtype = self.length.dtype

        if self.voltage > 0:
            return self._cavity_rmatrix(energy)
        else:
            return base_rmatrix(
                length=self.length,
                k1=torch.tensor(0.0, device=device, dtype=dtype),
                hx=torch.tensor(0.0, device=device, dtype=dtype),
                tilt=torch.tensor(0.0, device=device, dtype=dtype),
                energy=energy,
            )

    def track(self, incoming: Beam) -> Beam:
        """
        Track particles through the cavity. The input can be a `ParameterBeam` or a
        `ParticleBeam`. For a cavity, this does a little more than just the transfer map
        multiplication done by most elements.

        :param incoming: Beam of particles entering the element.
        :return: Beam of particles exiting the element.
        """
        if incoming is Beam.empty:
            return incoming
        elif isinstance(incoming, (ParameterBeam, ParticleBeam)):
            return self._track_beam(incoming)
        else:
            raise TypeError(f"Parameter incoming is of invalid type {type(incoming)}")

    def _track_beam(self, incoming: ParticleBeam) -> ParticleBeam:
        device = self.length.device
        dtype = self.length.dtype

        beta0 = torch.tensor(1.0, device=device, dtype=dtype)
        igamma2 = torch.tensor(0.0, device=device, dtype=dtype)
        g0 = torch.tensor(1e10, device=device, dtype=dtype)
        if incoming.energy != 0:
            g0 = incoming.energy / electron_mass_eV.to(device=device, dtype=dtype)
            igamma2 = 1 / g0**2
            beta0 = torch.sqrt(1 - igamma2)

        phi = torch.deg2rad(self.phase)

        tm = self.transfer_map(incoming.energy)
        if isinstance(incoming, ParameterBeam):
            outgoing_mu = torch.matmul(tm, incoming._mu)
            outgoing_cov = torch.matmul(tm, torch.matmul(incoming._cov, tm.t()))
        else:  # ParticleBeam
            outgoing_particles = torch.matmul(incoming.particles, tm.t())
        delta_energy = self.voltage * torch.cos(phi)

        T566 = 1.5 * self.length * igamma2 / beta0**3
        T556 = 0
        T555 = 0

        if incoming.energy + delta_energy > 0:
            k = 2 * torch.pi * self.frequency / constants.speed_of_light
            outgoing_energy = incoming.energy + delta_energy
            g1 = outgoing_energy / electron_mass_eV
            beta1 = torch.sqrt(1 - 1 / g1**2)

            if isinstance(incoming, ParameterBeam):
                outgoing_mu[5] = incoming._mu[5] * incoming.energy * beta0 / (
                    outgoing_energy * beta1
                ) + self.voltage * beta0 / (outgoing_energy * beta1) * (
                    torch.cos(-incoming._mu[4] * beta0 * k + phi) - torch.cos(phi)
                )
                outgoing_cov[5, 5] = incoming._cov[5, 5]

            else:  # ParticleBeam
                outgoing_particles[:, 5] = incoming.particles[
                    :, 5
                ] * incoming.energy * beta0 / (
                    outgoing_energy * beta1
                ) + self.voltage * beta0 / (
                    outgoing_energy * beta1
                ) * (
                    torch.cos(-incoming.particles[:, 4] * beta0 * k + phi)
                    - torch.cos(phi)
                )

            dgamma = self.voltage / electron_mass_eV
            if delta_energy > 0:
                T566 = (
                    self.length
                    * (beta0**3 * g0**3 - beta1**3 * g1**3)
                    / (2 * beta0 * beta1**3 * g0 * (g0 - g1) * g1**3)
                )
                T556 = (
                    beta0
                    * k
                    * self.length
                    * dgamma
                    * g0
                    * (beta1**3 * g1**3 + beta0 * (g0 - g1**3))
                    * torch.sin(phi)
                    / (beta1**3 * g1**3 * (g0 - g1) ** 2)
                )
                T555 = (
                    beta0**2
                    * k**2
                    * self.length
                    * dgamma
                    / 2.0
                    * (
                        dgamma
                        * (
                            2 * g0 * g1**3 * (beta0 * beta1**3 - 1)
                            + g0**2
                            + 3 * g1**2
                            - 2
                        )
                        / (beta1**3 * g1**3 * (g0 - g1) ** 3)
                        * torch.sin(phi) ** 2
                        - (g1 * g0 * (beta1 * beta0 - 1) + 1)
                        / (beta1 * g1 * (g0 - g1) ** 2)
                        * torch.cos(phi)
                    )
                )

            if isinstance(incoming, ParameterBeam):
                outgoing_mu[4] = incoming._mu[4] + (
                    T566 * incoming._mu[5] ** 2
                    + T556 * incoming._mu[4] * incoming._mu[5]
                    + T555 * incoming._mu[4] ** 2
                )
                outgoing_cov[4, 4] = (
                    T566 * incoming._cov[5, 5] ** 2
                    + T556 * incoming._cov[4, 5] * incoming._cov[5, 5]
                    + T555 * incoming._cov[4, 4] ** 2
                )
                outgoing_cov[4, 5] = (
                    T566 * incoming._cov[5, 5] ** 2
                    + T556 * incoming._cov[4, 5] * incoming._cov[5, 5]
                    + T555 * incoming._cov[4, 4] ** 2
                )
                outgoing_cov[5, 4] = outgoing_cov[4, 5]
            else:  # ParticleBeam
                outgoing_particles[:, 4] = incoming.particles[:, 4] + (
                    T566 * incoming.particles[:, 5] ** 2
                    + T556 * incoming.particles[:, 4] * incoming.particles[:, 5]
                    + T555 * incoming.particles[:, 4] ** 2
                )

        if isinstance(incoming, ParameterBeam):
            outgoing = ParameterBeam(
                outgoing_mu,
                outgoing_cov,
                outgoing_energy,
                total_charge=incoming.total_charge,
                device=outgoing_mu.device,
                dtype=outgoing_mu.dtype,
            )
            return outgoing
        else:  # ParticleBeam
            outgoing = ParticleBeam(
                outgoing_particles,
                outgoing_energy,
                particle_charges=incoming.particle_charges,
                device=outgoing_particles.device,
                dtype=outgoing_particles.dtype,
            )
            return outgoing

    def _cavity_rmatrix(self, energy: torch.Tensor) -> torch.Tensor:
        """Produces an R-matrix for a cavity when it is on, i.e. voltage > 0.0."""
        device = self.length.device
        dtype = self.length.dtype

        phi = torch.deg2rad(self.phase)
        delta_energy = self.voltage * torch.cos(phi)
        # Comment from Ocelot: Pure pi-standing-wave case
        eta = torch.tensor(1.0, device=device, dtype=dtype)
        Ei = energy / electron_mass_eV
        Ef = (energy + delta_energy) / electron_mass_eV
        Ep = (Ef - Ei) / self.length  # Derivative of the energy
        assert Ei > 0, "Initial energy must be larger than 0"

        alpha = torch.sqrt(eta / 8) / torch.cos(phi) * torch.log(Ef / Ei)

        r11 = torch.cos(alpha) - torch.sqrt(2 / eta) * torch.cos(phi) * torch.sin(alpha)

        # In Ocelot r12 is defined as below only if abs(Ep) > 10, and self.length
        # otherwise. This is implemented differently here in order to achieve results
        # closer to Bmad.
        r12 = torch.sqrt(8 / eta) * Ei / Ep * torch.cos(phi) * torch.sin(alpha)

        r21 = (
            -Ep
            / Ef
            * (
                torch.cos(phi) / torch.sqrt(2 * eta)
                + torch.sqrt(eta / 8) / torch.cos(phi)
            )
            * torch.sin(alpha)
        )

        r22 = (
            Ei
            / Ef
            * (
                torch.cos(alpha)
                + torch.sqrt(2 / eta) * torch.cos(phi) * torch.sin(alpha)
            )
        )

        r56 = torch.tensor(0.0)
        beta0 = torch.tensor(1.0)
        beta1 = torch.tensor(1.0)

        k = 2 * torch.pi * self.frequency / torch.tensor(constants.speed_of_light)
        r55_cor = 0
        if self.voltage != 0 and energy != 0:  # TODO: Do we need this if?
            beta0 = torch.sqrt(1 - 1 / Ei**2)
            beta1 = torch.sqrt(1 - 1 / Ef**2)

            r56 = -self.length / (Ef**2 * Ei * beta1) * (Ef + Ei) / (beta1 + beta0)
            g0 = Ei
            g1 = Ef
            r55_cor = (
                k
                * self.length
                * beta0
                * self.voltage
                / electron_mass_eV
                * torch.sin(phi)
                * (g0 * g1 * (beta0 * beta1 - 1) + 1)
                / (beta1 * g1 * (g0 - g1) ** 2)
            )

        r66 = Ei / Ef * beta0 / beta1
        r65 = k * torch.sin(phi) * self.voltage / (Ef * beta1 * electron_mass_eV)

        R = torch.eye(7, device=device, dtype=dtype)
        R[0, 0] = r11
        R[0, 1] = r12
        R[1, 0] = r21
        R[1, 1] = r22
        R[2, 2] = r11
        R[2, 3] = r12
        R[3, 2] = r21
        R[3, 3] = r22
        R[4, 4] = 1 + r55_cor
        R[4, 5] = r56
        R[5, 4] = r65
        R[5, 5] = r66

        return R

    def split(self, resolution: torch.Tensor) -> list[Element]:
        # TODO: Implement splitting for cavity properly, for now just returns the
        # element itself
        return [self]

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        alpha = 1 if self.is_active else 0.2
        height = 0.4

        patch = Rectangle(
            (s, 0), self.length, height, color="gold", alpha=alpha, zorder=2
        )
        ax.add_patch(patch)

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + ["length", "voltage", "phase", "frequency"]

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(length={repr(self.length)}, "
            + f"voltage={repr(self.voltage)}, "
            + f"phase={repr(self.phase)}, "
            + f"frequency={repr(self.frequency)}, "
            + f"name={repr(self.name)})"
        )


class BPM(Element):
    """
    Beam Position Monitor (BPM) in a particle accelerator.

    :param is_active: If `True` the BPM is active and will record the beam's position.
        If `False` the BPM is inactive and will not record the beam's position.
    :param name: Unique identifier of the element.
    """

    def __init__(self, is_active: bool = False, name: Optional[str] = None) -> None:
        super().__init__(name=name)

        self.is_active = is_active
        self.reading = None

    @property
    def is_skippable(self) -> bool:
        return not self.is_active

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        return torch.eye(7, device=energy.device, dtype=energy.dtype)

    def track(self, incoming: Beam) -> Beam:
        if incoming is Beam.empty:
            self.reading = None
        elif isinstance(incoming, ParameterBeam):
            self.reading = torch.stack([incoming.mu_x, incoming.mu_y])
        elif isinstance(incoming, ParticleBeam):
            self.reading = torch.stack([incoming.mu_x, incoming.mu_y])
        else:
            raise TypeError(f"Parameter incoming is of invalid type {type(incoming)}")

        return deepcopy(incoming)

    def split(self, resolution: torch.Tensor) -> list[Element]:
        return [self]

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        alpha = 1 if self.is_active else 0.2
        patch = Rectangle(
            (s, -0.3), 0, 0.3 * 2, color="darkkhaki", alpha=alpha, zorder=2
        )
        ax.add_patch(patch)

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={repr(self.name)})"


class Marker(Element):
    """
    General Marker / Monitor element

    :param name: Unique identifier of the element.
    """

    def __init__(self, name: Optional[str] = None) -> None:
        super().__init__(name=name)

    def transfer_map(self, energy):
        return torch.eye(7, device=energy.device, dtype=energy.dtype)

    def track(self, incoming):
        # TODO: At some point Markers should be able to be active or inactive. Active
        # Markers would be able to record the beam tracked through them.
        return incoming

    @property
    def is_skippable(self) -> bool:
        return True

    def split(self, resolution: torch.Tensor) -> list[Element]:
        return [self]

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        # Do nothing on purpose. Maybe later we decide markers should be shown, but for
        # now they are invisible.
        pass

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={repr(self.name)})"


class Screen(Element):
    """
    Diagnostic screen in a particle accelerator.

    :param resolution: Resolution of the camera sensor looking at the screen given as
        Tensor `(width, height)`.
    :param pixel_size: Size of a pixel on the screen in meters given as a Tensor
        `(width, height)`.
    :param binning: Binning used by the camera.
    :param misalignment: Misalignment of the screen in meters given as a Tensor
        `(x, y)`.
    :param is_active: If `True` the screen is active and will record the beam's
        distribution. If `False` the screen is inactive and will not record the beam's
        distribution.
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        resolution: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        pixel_size: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        binning: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        misalignment: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        is_active: bool = False,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.resolution = (
            torch.as_tensor(resolution, device=device)
            if resolution is not None
            else torch.tensor((1024, 1024), device=device)
        )
        self.pixel_size = (
            torch.as_tensor(pixel_size, **factory_kwargs)
            if pixel_size is not None
            else torch.tensor((1e-3, 1e-3), **factory_kwargs)
        )
        self.binning = (
            torch.as_tensor(binning, device=device)
            if binning is not None
            else torch.tensor(1, device=device)
        )
        self.misalignment = (
            torch.as_tensor(misalignment, **factory_kwargs)
            if misalignment is not None
            else torch.tensor((0.0, 0.0), **factory_kwargs)
        )
        self.is_active = is_active

        self.set_read_beam(None)
        self.cached_reading = None

    @property
    def is_skippable(self) -> bool:
        return not self.is_active

    @property
    def effective_resolution(self) -> torch.Tensor:
        return self.resolution / self.binning

    @property
    def effective_pixel_size(self) -> torch.Tensor:
        return self.pixel_size * self.binning

    @property
    def extent(self) -> torch.Tensor:
        return torch.stack(
            [
                -self.resolution[0] * self.pixel_size[0] / 2,
                self.resolution[0] * self.pixel_size[0] / 2,
                -self.resolution[1] * self.pixel_size[1] / 2,
                self.resolution[1] * self.pixel_size[1] / 2,
            ]
        )

    @property
    def pixel_bin_edges(self) -> tuple[torch.Tensor, torch.Tensor]:
        return (
            torch.linspace(
                -self.resolution[0] * self.pixel_size[0] / 2,
                self.resolution[0] * self.pixel_size[0] / 2,
                int(self.effective_resolution[0]) + 1,
            ),
            torch.linspace(
                -self.resolution[1] * self.pixel_size[1] / 2,
                self.resolution[1] * self.pixel_size[1] / 2,
                int(self.effective_resolution[1]) + 1,
            ),
        )

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        device = self.misalignment.device
        dtype = self.misalignment.dtype

        return torch.eye(7, device=device, dtype=dtype)

    def track(self, incoming: Beam) -> Beam:
        if self.is_active:
            copy_of_incoming = deepcopy(incoming)

            if isinstance(incoming, ParameterBeam):
                copy_of_incoming._mu[0] -= self.misalignment[0]
                copy_of_incoming._mu[2] -= self.misalignment[1]
            elif isinstance(incoming, ParticleBeam):
                copy_of_incoming.particles[:, 0] -= self.misalignment[0]
                copy_of_incoming.particles[:, 1] -= self.misalignment[1]

            self.set_read_beam(copy_of_incoming)

            return Beam.empty
        else:
            return incoming

    @property
    def reading(self) -> torch.Tensor:
        if self.cached_reading is not None:
            return self.cached_reading

        read_beam = self.get_read_beam()
        if read_beam is Beam.empty or read_beam is None:
            image = torch.zeros(
                (int(self.effective_resolution[1]), int(self.effective_resolution[0]))
            )
        elif isinstance(read_beam, ParameterBeam):
            transverse_mu = torch.stack([read_beam._mu[0], read_beam._mu[2]])
            transverse_cov = torch.stack(
                [
                    torch.stack([read_beam._cov[0, 0], read_beam._cov[0, 2]]),
                    torch.stack([read_beam._cov[2, 0], read_beam._cov[2, 2]]),
                ]
            )
            dist = MultivariateNormal(
                loc=transverse_mu.cpu(), covariance_matrix=transverse_cov.cpu()
            )

            left = self.extent[0]
            right = self.extent[1]
            hstep = self.pixel_size[0] * self.binning
            bottom = self.extent[2]
            top = self.extent[3]
            vstep = self.pixel_size[1] * self.binning
            x, y = torch.meshgrid(
                torch.arange(left, right, hstep),
                torch.arange(bottom, top, vstep),
                indexing="ij",
            )
            pos = torch.dstack((x, y))
            image = dist.log_prob(pos).exp()
            image = torch.flipud(image.T)
        elif isinstance(read_beam, ParticleBeam):
            image, _ = torch.histogramdd(
                torch.stack((read_beam.xs, read_beam.ys)).T.cpu(),
                bins=self.pixel_bin_edges,
            )
            image = torch.flipud(image.T)
            image = image.cpu()
        else:
            raise TypeError(f"Read beam is of invalid type {type(read_beam)}")

        self.cached_reading = image
        return image

    def get_read_beam(self) -> Beam:
        # Using these get and set methods instead of Python's property decorator to
        # prevent `nn.Module` from intercepting the read beam, which is itself an
        # `nn.Module`, and registering it as a submodule of the screen.
        return self._read_beam[0] if self._read_beam is not None else None

    def set_read_beam(self, value: Beam) -> None:
        # Using these get and set methods instead of Python's property decorator to
        # prevent `nn.Module` from intercepting the read beam, which is itself an
        # `nn.Module`, and registering it as a submodule of the screen.
        self._read_beam = [value]
        self.cached_reading = None

    def split(self, resolution: torch.Tensor) -> list[Element]:
        return [self]

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        alpha = 1 if self.is_active else 0.2
        patch = Rectangle(
            (s, -0.6), 0, 0.6 * 2, color="tab:green", alpha=alpha, zorder=2
        )
        ax.add_patch(patch)

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + [
            "resolution",
            "pixel_size",
            "binning",
            "misalignment",
            "is_active",
        ]

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(resolution={repr(self.resolution)}, "
            + f"pixel_size={repr(self.pixel_size)}, "
            + f"binning={repr(self.binning)}, "
            + f"misalignment={repr(self.misalignment)}, "
            + f"is_active={repr(self.is_active)}, "
            + f"name={repr(self.name)})"
        )


class Aperture(Element):
    """
    Physical aperture.

    :param x_max: half size horizontal offset in [m]
    :param y_max: half size vertical offset in [m]
    :param shape: Shape of the aperture. Can be "rectangular" or "elliptical".
    :param is_active: If the aperture actually blocks particles.
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        x_max: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        y_max: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        shape: Literal["rectangular", "elliptical"] = "rectangular",
        is_active: bool = True,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.x_max = (
            torch.as_tensor(x_max, **factory_kwargs)
            if x_max is not None
            else torch.tensor(float("inf"), **factory_kwargs)
        )
        self.y_max = (
            torch.as_tensor(y_max, **factory_kwargs)
            if y_max is not None
            else torch.tensor(float("inf"), **factory_kwargs)
        )
        self.shape = shape
        self.is_active = is_active

        self.lost_particles = None

    @property
    def is_skippable(self) -> bool:
        return not self.is_active

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        device = self.x_max.device
        dtype = self.x_max.dtype

        return torch.eye(7, device=device, dtype=dtype)

    def track(self, incoming: Beam) -> Beam:
        # Only apply aperture to particle beams and if the element is active
        if not (isinstance(incoming, ParticleBeam) and self.is_active):
            return incoming

        assert self.x_max >= 0 and self.y_max >= 0
        assert self.shape in [
            "rectangular",
            "elliptical",
        ], f"Unknown aperture shape {self.shape}"

        if self.shape == "rectangular":
            survived_mask = torch.logical_and(
                torch.logical_and(incoming.xs > -self.x_max, incoming.xs < self.x_max),
                torch.logical_and(incoming.ys > -self.y_max, incoming.ys < self.y_max),
            )
        elif self.shape == "elliptical":
            survived_mask = (
                incoming.xs**2 / self.x_max**2 + incoming.ys**2 / self.y_max**2
            ) <= 1.0
        outgoing_particles = incoming.particles[survived_mask]

        outgoing_particle_charges = incoming.particle_charges[survived_mask]

        self.lost_particles = incoming.particles[torch.logical_not(survived_mask)]

        self.lost_particle_charges = incoming.particle_charges[
            torch.logical_not(survived_mask)
        ]

        return (
            ParticleBeam(
                outgoing_particles,
                incoming.energy,
                particle_charges=outgoing_particle_charges,
                device=outgoing_particles.device,
                dtype=outgoing_particles.dtype,
            )
            if outgoing_particles.shape[0] > 0
            else ParticleBeam.empty
        )

    def split(self, resolution: torch.Tensor) -> list[Element]:
        # TODO: Implement splitting for aperture properly, for now just return self
        return [self]

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        alpha = 1 if self.is_active else 0.2
        height = 0.4

        dummy_length = 0.0

        patch = Rectangle(
            (s, 0), dummy_length, height, color="tab:pink", alpha=alpha, zorder=2
        )
        ax.add_patch(patch)

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + [
            "x_max",
            "y_max",
            "shape",
            "is_active",
        ]

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(x_max={repr(self.x_max)}, "
            + f"y_max={repr(self.y_max)}, "
            + f"shape={repr(self.shape)}, "
            + f"is_active={repr(self.is_active)}, "
            + f"name={repr(self.name)})"
        )


class Undulator(Element):
    """
    Element representing an undulator in a particle accelerator.

    NOTE Currently behaves like a drift section but is plotted distinctively.

    :param length: Length in meters.
    :param is_active: Indicates if the undulator is active or not. Currently has no
        effect.
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        length: Union[torch.Tensor, nn.Parameter],
        is_active: bool = False,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.length = torch.as_tensor(length, **factory_kwargs)
        self.is_active = is_active

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        device = self.length.device
        dtype = self.length.dtype

        gamma = energy / rest_energy.to(device=device, dtype=dtype)
        igamma2 = (
            1 / gamma**2
            if gamma != 0
            else torch.tensor(0.0, device=device, dtype=dtype)
        )

        tm = torch.eye(7, device=device, dtype=dtype)
        tm[0, 1] = self.length
        tm[2, 3] = self.length
        tm[4, 5] = self.length * igamma2

        return tm

    @property
    def is_skippable(self) -> bool:
        return True

    def split(self, resolution: torch.Tensor) -> list[Element]:
        # TODO: Implement splitting for undulator properly, for now just return self
        return [self]

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        alpha = 1 if self.is_active else 0.2
        height = 0.4

        patch = Rectangle(
            (s, 0), self.length, height, color="tab:purple", alpha=alpha, zorder=2
        )
        ax.add_patch(patch)

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + ["length"]

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(length={repr(self.length)}, "
            + f"is_active={repr(self.is_active)}, "
            + f"name={repr(self.name)})"
        )


class Solenoid(Element):
    """
    Solenoid magnet.

    Implemented according to A.W.Chao P74

    :param length: Length in meters.
    :param k: Normalised strength of the solenoid magnet B0/(2*Brho). B0 is the field
        inside the solenoid, Brho is the momentum of central trajectory.
    :param misalignment: Misalignment vector of the solenoid magnet in x- and
        y-directions.
    :param name: Unique identifier of the element.
    """

    def __init__(
        self,
        length: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        k: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        misalignment: Optional[Union[torch.Tensor, nn.Parameter]] = None,
        name: Optional[str] = None,
        device=None,
        dtype=torch.float32,
    ) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__(name=name)

        self.length = (
            torch.as_tensor(length, **factory_kwargs)
            if length is not None
            else torch.tensor(0.0, **factory_kwargs)
        )
        self.k = (
            torch.as_tensor(k, **factory_kwargs)
            if k is not None
            else torch.tensor(0.0, **factory_kwargs)
        )
        self.misalignment = (
            torch.as_tensor(misalignment, **factory_kwargs)
            if misalignment is not None
            else torch.tensor((0.0, 0.0), **factory_kwargs)
        )

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        device = self.length.device
        dtype = self.length.dtype

        gamma = energy / rest_energy.to(device=device, dtype=dtype)
        c = torch.cos(self.length * self.k)
        s = torch.sin(self.length * self.k)
        if self.k == 0:
            s_k = self.length
        else:
            s_k = s / self.k
        r56 = torch.tensor(0.0, device=device, dtype=dtype)
        if gamma != 0:
            gamma2 = gamma * gamma
            beta = torch.sqrt(1.0 - 1.0 / gamma2)
            r56 -= self.length / (beta * beta * gamma2)

        R = torch.eye(7, device=device, dtype=dtype)
        R[0, 0] = c**2
        R[0, 1] = c * s_k
        R[0, 2] = s * c
        R[0, 3] = s * s_k
        R[1, 0] = -self.k * s * c
        R[1, 1] = c**2
        R[1, 2] = -self.k * s**2
        R[1, 3] = s * c
        R[2, 0] = -s * c
        R[2, 1] = -s * s_k
        R[2, 2] = c**2
        R[2, 3] = c * s_k
        R[3, 0] = self.k * s**2
        R[3, 1] = -s * c
        R[3, 2] = -self.k * s * c
        R[3, 3] = c**2
        R[4, 5] = r56

        R = R.real

        if self.misalignment[0] == 0 and self.misalignment[1] == 0:
            return R
        else:
            R_exit, R_entry = misalignment_matrix(self.misalignment)
            R = torch.matmul(R_exit, torch.matmul(R, R_entry))
            return R

    @property
    def is_active(self) -> bool:
        return self.k != 0

    def is_skippable(self) -> bool:
        return True

    def split(self, resolution: torch.Tensor) -> list[Element]:
        # TODO: Implement splitting for solenoid properly, for now just return self
        return [self]

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        alpha = 1 if self.is_active else 0.2
        height = 0.8

        patch = Rectangle(
            (s, 0), self.length, height, color="tab:orange", alpha=alpha, zorder=2
        )
        ax.add_patch(patch)

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + ["length", "k", "misalignment"]

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(length={repr(self.length)}, "
            + f"k={repr(self.k)}, "
            + f"misalignment={repr(self.misalignment)}, "
            + f"name={repr(self.name)})"
        )


class Segment(Element):
    """
    Segment of a particle accelerator consisting of several elements.

    :param cell: List of Cheetah elements that describe an accelerator (section).
    :param name: Unique identifier of the element.
    """

    def __init__(self, elements: list[Element], name: str = "unnamed") -> None:
        super().__init__(name=name)

        self.elements = nn.ModuleList(elements)

        for element in self.elements:
            # Make elements accessible via .name attribute. If multiple elements have
            # the same name, they are accessible via a list.
            if element.name in self.__dict__:
                if isinstance(self.__dict__[element.name], list):
                    self.__dict__[element.name].append(element)
                else:  # Is instance of cheetah.Element
                    self.__dict__[element.name] = [self.__dict__[element.name], element]
            else:
                self.__dict__[element.name] = element

    def subcell(self, start: str, end: str) -> "Segment":
        """Extract a subcell `[start, end]` from an this segment."""
        subcell = []
        is_in_subcell = False
        for element in self.elements:
            if element.name == start:
                is_in_subcell = True
            if is_in_subcell:
                subcell.append(element)
            if element.name == end:
                break

        return self.__class__(subcell)

    def flattened(self) -> "Segment":
        """
        Return a flattened version of the segment, i.e. one where all subsegments are
        resolved and their elements entered into a top-level segment.
        """
        flattened_elements = []
        for element in self.elements:
            if isinstance(element, Segment):
                flattened_elements += element.flattened().elements
            else:
                flattened_elements.append(element)

        return Segment(elements=flattened_elements, name=self.name)

    def transfer_maps_merged(
        self, incoming_beam: Beam, except_for: Optional[list[str]] = None
    ) -> "Segment":
        """
        Return a segment where the transfer maps of skipable elements are merged into
        elements of type `CustomTransferMap`. This can be used to speed up tracking
        through the segment.

        :param incoming_beam: Beam that is incoming to the segment. NOTE: This beam is
            needed to determine the energy of the beam when entering each element, as
            the transfer maps of merged elements might depend on the beam energy.
        :param except_for: List of names of elements that should not be merged despite
            being skippable. Usually these are the elements that are changed from one
            tracking to another.
        :return: Segment with merged transfer maps.
        """
        if except_for is None:
            except_for = []

        merged_elements = []  # Elements for new merged segment
        skippable_elements = []  # Keep track of elements that are not yet merged
        tracked_beam = incoming_beam
        for element in self.elements:
            if element.is_skippable and element.name not in except_for:
                skippable_elements.append(element)
            else:
                if len(skippable_elements) == 1:
                    merged_elements.append(skippable_elements[0])
                    tracked_beam = skippable_elements[0].track(tracked_beam)
                elif len(skippable_elements) > 1:  # i.e. we need to merge some elements
                    merged_elements.append(
                        CustomTransferMap.from_merging_elements(
                            skippable_elements, incoming_beam=tracked_beam
                        )
                    )
                    tracked_beam = merged_elements[-1].track(tracked_beam)
                skippable_elements = []

                merged_elements.append(element)
                tracked_beam = element.track(tracked_beam)

        if len(skippable_elements) > 0:
            merged_elements.append(
                CustomTransferMap.from_merging_elements(
                    skippable_elements, incoming_beam=tracked_beam
                )
            )

        return Segment(elements=merged_elements, name=self.name)

    def without_inactive_markers(
        self, except_for: Optional[list[str]] = None
    ) -> "Segment":
        """
        Return a segment where all inactive markers are removed. This can be used to
        speed up tracking through the segment.

        NOTE: `is_active` has not yet been implemented for Markers. Therefore, this
        function currently removes all markers.

        :param except_for: List of names of elements that should not be removed despite
            being inactive.
        :return: Segment without inactive markers.
        """
        # TODO: Add check for is_active once that has been implemented for Markers
        if except_for is None:
            except_for = []

        return Segment(
            elements=[
                element
                for element in self.elements
                if not isinstance(element, Marker) or element.name in except_for
            ],
            name=self.name,
        )

    def without_inactive_zero_length_elements(
        self, except_for: Optional[list[str]] = None
    ) -> "Segment":
        """
        Return a segment where all inactive zero length elements are removed. This can
        be used to speed up tracking through the segment.

        NOTE: If `is_active` is not implemented for an element, it is assumed to be
        inactive and will be removed.

        :param except_for: List of names of elements that should not be removed despite
            being inactive and having a zero length.
        :return: Segment without inactive zero length elements.
        """
        if except_for is None:
            except_for = []

        return Segment(
            elements=[
                element
                for element in self.elements
                if (hasattr(element, "length") and element.length > 0.0)
                or (hasattr(element, "is_active") and element.is_active)
                or element.name in except_for
            ],
            name=self.name,
        )

    def inactive_elements_as_drifts(
        self, except_for: Optional[list[str]] = None
    ) -> "Segment":
        """
        Return a segment where all inactive elements (that have a length) are replaced
        by drifts. This can be used to speed up tracking through the segment and is a
        valid thing to as inactive elements should basically be no different from drift
        sections.

        :param except_for: List of names of elements that should not be replaced by
            drifts despite being inactive. Usually these are the elements that are
            currently inactive but will be activated later.
        :return: Segment with inactive elements replaced by drifts.
        """
        if except_for is None:
            except_for = []

        return Segment(
            elements=[
                (
                    element
                    if (hasattr(element, "is_active") and element.is_active)
                    or not hasattr(element, "length")
                    or element.name in except_for
                    else Drift(element.length)
                )
                for element in self.elements
            ],
            name=self.name,
        )

    @classmethod
    def from_lattice_json(cls, filepath: str) -> "Segment":
        """
        Load a Cheetah model from a JSON file.

        :param filename: Name/path of the file to load the lattice from.
        :return: Loaded Cheetah `Segment`.
        """
        return load_cheetah_model(filepath)

    def to_lattice_json(
        self,
        filepath: str,
        title: Optional[str] = None,
        info: str = "This is a placeholder lattice description",
    ) -> None:
        """
        Save a Cheetah model to a JSON file.

        :param filename: Name/path of the file to save the lattice to.
        :param title: Title of the lattice. If not provided, defaults to the name of the
            `Segment` object. If that also does not have a name, defaults to "Unnamed
            Lattice".
        :param info: Information about the lattice. Defaults to "This is a placeholder
            lattice description".
        """
        save_cheetah_model(self, filepath, title, info)

    @classmethod
    def from_ocelot(
        cls,
        cell,
        name: Optional[str] = None,
        warnings: bool = True,
        device=None,
        dtype=torch.float32,
        **kwargs,
    ) -> "Segment":
        """
        Translate an Ocelot cell to a Cheetah `Segment`.

        NOTE Objects not supported by Cheetah are translated to drift sections. Screen
        objects are created only from `ocelot.Monitor` objects when the string "BSC" is
        contained in their `id` attribute. Their screen properties are always set to
        default values and most likely need adjusting afterwards. BPM objects are only
        created from `ocelot.Monitor` objects when their id has a substring "BPM".

        :param cell: Ocelot cell, i.e. a list of Ocelot elements to be converted.
        :param name: Unique identifier for the entire segment.
        :param warnings: Whether to print warnings when objects are not supported by
            Cheetah or converted with potentially unexpected behavior.
        :return: Cheetah segment closely resembling the Ocelot cell.
        """
        from cheetah.converters.nocelot import ocelot2cheetah

        converted = [
            ocelot2cheetah(element, warnings=warnings, device=device, dtype=dtype)
            for element in cell
        ]
        return cls(converted, name=name, **kwargs)

    @classmethod
    def from_bmad(
        cls, bmad_lattice_file_path: str, environment_variables: Optional[dict] = None
    ) -> "Segment":
        """
        Read a Cheetah segment from a Bmad lattice file.

        NOTE: This function was designed at the example of the LCLS lattice. While this
        lattice is extensive, this function might not properly convert all features of
        a Bmad lattice. If you find that this function does not work for your lattice,
        please open an issue on GitHub.

        :param bmad_lattice_file_path: Path to the Bmad lattice file.
        :param environment_variables: Dictionary of environment variables to use when
            parsing the lattice file.
        :return: Cheetah `Segment` representing the Bmad lattice.
        """
        bmad_lattice_file_path = Path(bmad_lattice_file_path)
        return convert_bmad_lattice(bmad_lattice_file_path, environment_variables)

    @classmethod
    def from_nx_tables(cls, filepath: Union[Path, str]) -> "Element":
        """
        Read an NX Tables CSV-like file generated for the ARES lattice into a Cheetah
        `Segment`.

        NOTE: This format is specific to the ARES accelerator at DESY.

        :param filepath: Path to the NX Tables file.
        :return: Converted Cheetah `Segment`.
        """
        if isinstance(filepath, str):
            filepath = Path(filepath)

        return read_nx_tables(filepath)

    @property
    def is_skippable(self) -> bool:
        return all(element.is_skippable for element in self.elements)

    @property
    def length(self) -> torch.Tensor:
        lengths = torch.stack(
            [element.length for element in self.elements if hasattr(element, "length")]
        )
        return torch.sum(lengths)

    def transfer_map(self, energy: torch.Tensor) -> torch.Tensor:
        if self.is_skippable:
            tm = torch.eye(7, device=energy.device, dtype=energy.dtype)
            for element in self.elements:
                tm = torch.matmul(element.transfer_map(energy), tm)
            return tm
        else:
            return None

    def track(self, incoming: Beam) -> Beam:
        if self.is_skippable:
            return super().track(incoming)
        else:
            todos = []
            for element in self.elements:
                if not element.is_skippable:
                    todos.append(element)
                elif not todos or not todos[-1].is_skippable:
                    todos.append(Segment([element]))
                else:
                    todos[-1].elements.append(element)

            for todo in todos:
                incoming = todo.track(incoming)

            return incoming

    def split(self, resolution: torch.Tensor) -> list[Element]:
        return [
            split_element
            for element in self.elements
            for split_element in element.split(resolution)
        ]

    def plot(self, ax: matplotlib.axes.Axes, s: float) -> None:
        element_lengths = [
            element.length if hasattr(element, "length") else 0.0
            for element in self.elements
        ]
        element_ss = [0] + [
            sum(element_lengths[: i + 1]) for i, _ in enumerate(element_lengths)
        ]
        element_ss = [s + element_s for element_s in element_ss]

        ax.plot([0, element_ss[-1]], [0, 0], "--", color="black")

        for element, s in zip(self.elements, element_ss[:-1]):
            element.plot(ax, s)

        ax.set_ylim(-1, 1)
        ax.set_xlabel("s (m)")
        ax.set_yticks([])

    def plot_reference_particle_traces(
        self,
        axx: matplotlib.axes.Axes,
        axy: matplotlib.axes.Axes,
        beam: Optional[Beam] = None,
        num_particles: int = 10,
        resolution: float = 0.01,
    ) -> None:
        """
        Plot `n` reference particles along the segment view in x- and y-direction.

        :param axx: Axes to plot the particle traces into viewed in x-direction.
        :param axy: Axes to plot the particle traces into viewed in y-direction.
        :param beam: Entering beam from which the reference particles are sampled.
        :param num_particles: Number of reference particles to plot. Must not be larger
            than number of particles passed in `beam`.
        :param resolution: Minimum resolution of the tracking of the reference particles
            in the plot.
        """
        reference_segment = deepcopy(self)
        splits = reference_segment.split(resolution=torch.tensor(resolution))

        split_lengths = [
            split.length if hasattr(split, "length") else 0.0 for split in splits
        ]
        ss = [0] + [sum(split_lengths[: i + 1]) for i, _ in enumerate(split_lengths)]

        references = []
        if beam is None:
            initial = ParticleBeam.make_linspaced(
                num_particles=num_particles, device="cpu"
            )
            references.append(initial)
        else:
            initial = ParticleBeam.make_linspaced(
                num_particles=num_particles,
                mu_x=beam.mu_x,
                mu_xp=beam.mu_xp,
                mu_y=beam.mu_y,
                mu_yp=beam.mu_yp,
                sigma_x=beam.sigma_x,
                sigma_xp=beam.sigma_xp,
                sigma_y=beam.sigma_y,
                sigma_yp=beam.sigma_yp,
                sigma_s=beam.sigma_s,
                sigma_p=beam.sigma_p,
                energy=beam.energy,
                device="cpu",
            )
            references.append(initial)
        for split in splits:
            sample = split(references[-1])
            references.append(sample)

        for particle_index in range(num_particles):
            xs = [
                float(reference_beam.xs[particle_index].cpu())
                for reference_beam in references
                if reference_beam is not Beam.empty
            ]
            axx.plot(ss[: len(xs)], xs)
        axx.set_xlabel("s (m)")
        axx.set_ylabel("x (m)")
        axx.grid()

        for particle_index in range(num_particles):
            ys = [
                float(reference_beam.ys[particle_index].cpu())
                for reference_beam in references
                if reference_beam is not Beam.empty
            ]
            axy.plot(ss[: len(ys)], ys)
        axx.set_xlabel("s (m)")
        axy.set_ylabel("y (m)")
        axy.grid()

    def plot_overview(
        self,
        fig: Optional[matplotlib.figure.Figure] = None,
        beam: Optional[Beam] = None,
        n: int = 10,
        resolution: float = 0.01,
    ) -> None:
        """
        Plot an overview of the segment with the lattice and traced reference particles.

        :param fig: Figure to plot the overview into.
        :param beam: Entering beam from which the reference particles are sampled.
        :param n: Number of reference particles to plot. Must not be larger than number
            of particles passed in `beam`.
        :param resolution: Minimum resolution of the tracking of the reference particles
            in the plot.
        """
        if fig is None:
            fig = plt.figure()
        gs = fig.add_gridspec(3, hspace=0, height_ratios=[2, 2, 1])
        axs = gs.subplots(sharex=True)

        axs[0].set_title("Reference Particle Traces")
        self.plot_reference_particle_traces(axs[0], axs[1], beam, n, resolution)

        self.plot(axs[2], 0)

        plt.tight_layout()

    def plot_twiss(self, beam: Beam, ax: Optional[Any] = None) -> None:
        """Plot twiss parameters along the segment."""
        longitudinal_beams = [beam]
        s_positions = [0.0]
        for element in self.elements:
            if not hasattr(element, "length") or element.length == 0:
                continue

            outgoing = element.track(longitudinal_beams[-1])

            longitudinal_beams.append(outgoing)
            s_positions.append(s_positions[-1] + element.length)

        beta_x = [beam.beta_x for beam in longitudinal_beams]
        beta_y = [beam.beta_y for beam in longitudinal_beams]

        if ax is None:
            fig = plt.figure()
            ax = fig.add_subplot(111)

        ax.set_title("Twiss Parameters")
        ax.set_xlabel("s (m)")
        ax.set_ylabel(r"$\beta$ (m)")

        ax.plot(s_positions, beta_x, label=r"$\beta_x$", c="tab:red")
        ax.plot(s_positions, beta_y, label=r"$\beta_y$", c="tab:green")

        ax.legend()
        plt.tight_layout()

    @property
    def defining_features(self) -> list[str]:
        return super().defining_features + ["elements"]

    def plot_twiss_over_lattice(self, beam: Beam, figsize=(8, 4)) -> None:
        """Plot twiss parameters in a plot over a plot of the lattice."""
        fig = plt.figure(figsize=figsize)
        gs = fig.add_gridspec(2, hspace=0, height_ratios=[3, 1])
        axs = gs.subplots(sharex=True)

        self.plot_twiss(beam, ax=axs[0])
        self.plot(axs[1], 0)

        plt.tight_layout()

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(elements={repr(self.elements)}, "
            + f"name={repr(self.name)})"
        )
