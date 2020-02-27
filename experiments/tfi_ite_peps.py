from itertools import chain
import argparse, time

from koala import statevector, peps, Observable
from koala.peps import sites

import tensorbackends
from tensorbackends.interface import ImplicitRandomizedSVD
import numpy as np
import scipy.linalg as sla


PAULI_X = np.array([[0,1],[1,0]], dtype=complex)
PAULI_Z = np.array([[1,0],[0,-1]], dtype=complex)
PAULI_ZZ = np.einsum('ij,kl->ikjl', PAULI_Z, PAULI_Z)


def one_site_operator(tsr):
    return tsr.reshape(1,1,1,1,2,2)

def horizontal_pair_site_operator(tsr):
    xy, s, uv = tsr.backend.einsvd('xyuv->xys,uvs', tsr)
    s = s ** 2
    xy = tsr.backend.einsum('xys,s->()s()()xy', xy, s)
    uv = tsr.backend.einsum('uvs,s->()()()suv', uv, s)
    return xy, uv

def vertical_pair_site_operator(tsr):
    xy, s, uv = tsr.backend.einsvd('xyuv->sxy,suv', tsr)
    s = s ** 2
    xy = tsr.backend.einsum('sxy,s->()()s()xy', xy, s)
    uv = tsr.backend.einsum('suv,s->s()()()uv', uv, s)
    return xy, uv

class Timer:
    def __init__(self, backend, name):
        backend = tensorbackends.get(backend)
        if backend.name in {'ctf', 'ctfview'}:
            import ctf
            self.timer = ctf.timer(name)
        else:
            self.timer = None

    def __enter__(self):
        if self.timer is not None:
            self.timer.start()

    def __exit__(self, type, value, traceback):
        if self.timer is not None:
            self.timer.stop() 


def build_tfi_trottered_step_operators(J, h, nrows, ncols, tau, backend):
    numpy_backend = tensorbackends.get('numpy')
    backend = tensorbackends.get(backend)
    one_site = one_site_operator(numpy_backend.astensor(sla.expm(PAULI_X*(h*tau))))
    exp_pauli_zz = numpy_backend.astensor(sla.expm(PAULI_ZZ.reshape(4,4)*(J*tau)).reshape(2,2,2,2))
    h_two_site = horizontal_pair_site_operator(exp_pauli_zz)
    v_two_site = vertical_pair_site_operator(exp_pauli_zz)
    hoperators = {}
    voperators = {}
    touched_sites = set()
    # horizontal two site operators
    for i, j in np.ndindex(nrows, ncols-1):
        a, b = (i,j), (i,j+1)
        op_a, op_b = h_two_site[0], h_two_site[1]
        if a not in touched_sites:
            op_a = sites.contract_z(one_site, op_a)
            touched_sites.add(a)
        if b not in touched_sites:
            op_b = sites.contract_z(one_site, op_b)
            touched_sites.add(b)
        hoperators[a, b] = op_a, op_b
    # vertical two site operators
    for i, j in np.ndindex(nrows-1, ncols):
        voperators[(i,j), (i+1,j)] = v_two_site[0], v_two_site[1]
    return {
        idx: (backend.astensor(a.unwrap()), backend.astensor(b.unwrap()))
        for idx, (a,b) in hoperators.items()
    }, {
        idx: (backend.astensor(a.unwrap()), backend.astensor(b.unwrap()))
        for idx, (a,b) in voperators.items()
    }


def apply_trottered_step_operators(qstate, hoperators, voperators, maxrank):
    for (a, b), (op_a, op_b) in hoperators.items():
        with Timer(qstate.backend, 'apply_trottered_step_operators_contraction'):
            qstate.grid[a] = sites.contract_z(qstate[a], op_a)
            qstate.grid[b] = sites.contract_z(qstate[b], op_b)
        with Timer(qstate.backend, 'reduce_bond_dimensions'):
            qstate.grid[a], qstate.grid[b] = sites.reduce_y(qstate[a], qstate[b], option=ImplicitRandomizedSVD(maxrank))
    for (a, b), (op_a, op_b) in voperators.items():
        with Timer(qstate.backend, 'apply_trottered_step_operators_contraction'):
            qstate.grid[a] = sites.contract_z(qstate[a], op_a)
            qstate.grid[b] = sites.contract_z(qstate[b], op_b)
        with Timer(qstate.backend, 'reduce_bond_dimensions'):
            qstate.grid[a], qstate.grid[b] = sites.reduce_x(qstate[a], qstate[b], option=ImplicitRandomizedSVD(maxrank))


def horizontal_links(nrows, ncols):
    # horizontal
    for i, j in np.ndindex(nrows, ncols-1):
        yield (i, j), (i, j+1)

def vertical_links(nrows, ncols):
    # vertical
    for i, j in np.ndindex(nrows-1, ncols):
        yield (i, j), (i+1, j)



def run_peps(operators, nrow, ncol, steps, normfreq, backend, threshold, maxrank, randomized_svd):
    using_ctf = backend in {'ctf', 'ctfview'}
    if using_ctf:
        import ctf
        timer_epoch = ctf.timer_epoch('run_peps')
        timer_epoch.begin()
        ctf.initialize_flops_counter()
    qstate = peps.random(nrow, ncol, maxrank, backend=backend)
    if using_ctf:
        ctf.initialize_flops_counter()
    for i in range(steps):
        with Timer(qstate.backend, 'apply_trottered_step_operators'):
            apply_trottered_step_operators(qstate, *operators, maxrank)
        if (i+1) % normfreq == 0:
            qstate.site_normalize()
    if using_ctf:
        timer_epoch.end()
        flops = ctf.get_estimated_flops()
    else:
        flops = None
    return qstate, flops


def main(args):
    with Timer(args.backend, 'build_operators'):
        operators = build_tfi_trottered_step_operators(args.coupling, args.field, args.nrow, args.ncol, args.tau, args.backend)

    t = time.time()
    peps_qstate, flops = run_peps(operators, args.nrow, args.ncol, args.steps, args.normfreq, backend=args.backend, threshold=args.threshold, maxrank=args.maxrank, randomized_svd=args.randomized_svd)
    peps_ite_time = time.time() - t

    backend = tensorbackends.get(args.backend)

    if backend.rank == 0:
        print('tfi.nrow', args.nrow)
        print('tfi.ncol', args.ncol)
        print('tfi.field', args.field)
        print('tfi.coupling', args.coupling)

        print('ite.steps', args.steps)
        print('ite.tau', args.tau)
        print('ite.normfreq', args.normfreq)

        print('backend.name', args.backend)
        print('backend.nproc', backend.nproc)

        print('peps.maxrank', args.maxrank)

        print('result.peps_ite_time', peps_ite_time)
        print('result.peps_ite_flops', flops)


def build_cli_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument('-r', '--nrow', help='the number of rows', type=int, default=3)
    parser.add_argument('-c', '--ncol', help='the number of columns', type=int, default=3)

    parser.add_argument('-cp', '--coupling', help='coupling value of TFI', type=float, default=1.0)
    parser.add_argument('-f', '--field', help='field value of TFI', type=float, default=0.2)

    parser.add_argument('-s', '--steps', help='ITE steps', type=int, default=100)
    parser.add_argument('-tau', help='ITE trotter size', type=float, default=0.01)
    parser.add_argument('-nf', '--normfreq', help='ITE normalization frequency', type=int, default=10)

    parser.add_argument('-b', '--backend', help='the backend to use', choices=['numpy', 'ctf', 'ctfview'], default='numpy')
    parser.add_argument('-th', '--threshold', help='the threshold in trucated SVD when applying gates', type=float, default=1e-5)
    parser.add_argument('-mr', '--maxrank', help='the maxrank in trucated SVD when applying gates', type=int, default=1)
    parser.add_argument('-rsvd', '--randomized_svd', help='use randomized SVD when applying gates', default=False, action='store_true')

    return parser


if __name__ == '__main__':
    parser = build_cli_parser()
    main(parser.parse_args())