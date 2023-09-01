from cheetah import Drift, ParameterBeam, ParticleBeam


def test_diverging_parameter_beam():
    """
    Test that that a parameter beam with sigma_xp > 0 and sigma_yp > 0 increases in
    size in both dimensions when travelling through a drift section.
    """
    drift = Drift(length=1.0)
    incoming_beam = ParameterBeam.from_parameters(sigma_xp=2e-7, sigma_yp=2e-7)
    outgoing_beam = drift(incoming_beam)

    assert outgoing_beam.sigma_x > incoming_beam.sigma_x
    assert outgoing_beam.sigma_y > incoming_beam.sigma_y


def test_diverging_particle_beam():
    """
    Test that that a particle beam with sigma_xp > 0 and sigma_yp > 0 increases in
    size in both dimensions when travelling through a drift section.
    """
    drift = Drift(length=1.0)
    incoming_beam = ParticleBeam.from_parameters(n=1000, sigma_xp=2e-7, sigma_yp=2e-7)
    outgoing_beam = drift(incoming_beam)

    assert outgoing_beam.sigma_x > incoming_beam.sigma_x
    assert outgoing_beam.sigma_y > incoming_beam.sigma_y
