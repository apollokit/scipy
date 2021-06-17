__all__ = ['svds']

import numpy as np

from .arpack import _arpack
arpack_int = _arpack.timing.nbx.dtype
from . import eigsh
from ._svds_doc import _svds_arpack_doc, _svds_lobpcg_doc

from scipy.sparse.linalg.interface import LinearOperator
from scipy.sparse import isspmatrix
from scipy.sparse.sputils import is_pydata_spmatrix
from scipy.sparse.linalg.eigen.lobpcg import lobpcg

def _augmented_orthonormal_cols(x, k):
    # extract the shape of the x array
    n, m = x.shape
    # create the expanded array and copy x into it
    y = np.empty((n, m+k), dtype=x.dtype)
    y[:, :m] = x
    # do some modified gram schmidt to add k random orthonormal vectors
    for i in range(k):
        # sample a random initial vector
        v = np.random.randn(n)
        if np.iscomplexobj(x):
            v = v + 1j*np.random.randn(n)
        # subtract projections onto the existing unit length vectors
        for j in range(m+i):
            u = y[:, j]
            v -= (np.dot(v, u.conj()) / np.dot(u, u.conj())) * u
        # normalize v
        v /= np.sqrt(np.dot(v, v.conj()))
        # add v into the output array
        y[:, m+i] = v
    # return the expanded array
    return y


def _augmented_orthonormal_rows(x, k):
    return _augmented_orthonormal_cols(x.T, k).T


def _herm(x):
    return x.T.conj()


def svds(A, k=6, ncv=None, tol=0, which='LM', v0=None,
         maxiter=None, return_singular_vectors=True,
         solver='arpack'):
    """
    Partial singular value decomposition of a sparse matrix.

    Compute the largest or smallest `k` singular values and corresponding
    singular vectors of a sparse matrix. The order in which the singular
    values are returned is not guaranteed.

    Parameters
    ----------
    A : {sparse matrix, LinearOperator}
        Array to compute the SVD on, of shape (M, N)
    k : int, optional
        Number of singular values and vectors to compute.
        Must be 1 <= k < min(A.shape).
    ncv : int, optional
        The number of Lanczos vectors generated
        ncv must be greater than k+1 and smaller than n;
        it is recommended that ncv > 2*k
        Default: ``min(n, max(2*k + 1, 20))``
    tol : float, optional
        Tolerance for singular values. Zero (default) means machine precision.
    which : str, ['LM' | 'SM'], optional
        Which `k` singular values to find:

            - 'LM' : largest singular values
            - 'SM' : smallest singular values

        .. versionadded:: 0.12.0
    v0 : ndarray, optional
        Starting vector for iteration, of length min(A.shape). Should be an
        (approximate) left singular vector if N > M and a right singular
        vector otherwise.
        Default: random

        .. versionadded:: 0.12.0
    maxiter : int, optional
        Maximum number of iterations.

        .. versionadded:: 0.12.0
    return_singular_vectors : bool or str, optional
        - True: return singular vectors (True) in addition to singular values.

        .. versionadded:: 0.12.0

        - "u": only return the u matrix, without computing vh (if N > M).
        - "vh": only return the vh matrix, without computing u (if N <= M).

        .. versionadded:: 0.16.0
    solver : str, optional
            The solver used.
            :ref:`'arpack' <sparse.linalg.svds-arpack>` and
            :ref:`'lobpcg' <sparse.linalg.svds-lobpcg>` are supported.
            Default: `'arpack'`.

    Returns
    -------
    u : ndarray, shape=(M, k)
        Unitary matrix having left singular vectors as columns.
        If `return_singular_vectors` is "vh", this variable is not computed,
        and None is returned instead.
    s : ndarray, shape=(k,)
        The singular values.
    vt : ndarray, shape=(k, N)
        Unitary matrix having right singular vectors as rows.
        If `return_singular_vectors` is "u", this variable is not computed,
        and None is returned instead.


    Notes
    -----
    This is a naive implementation using ARPACK or LOBPCG as an eigensolver
    on A.H * A or A * A.H, depending on which one is more efficient.

    Examples
    --------
    >>> from scipy.sparse import csc_matrix
    >>> from scipy.sparse.linalg import svds, eigs
    >>> A = csc_matrix([[1, 0, 0], [5, 0, 2], [0, -1, 0], [0, 0, 3]], dtype=float)
    >>> u, s, vt = svds(A, k=2)
    >>> s
    array([ 2.75193379,  5.6059665 ])
    >>> np.sqrt(eigs(A.dot(A.T), k=2)[0]).real
    array([ 5.6059665 ,  2.75193379])
    """
    if which == 'LM':
        largest = True
    elif which == 'SM':
        largest = False
    else:
        raise ValueError("which must be either 'LM' or 'SM'.")

    if not (isinstance(A, LinearOperator) or isspmatrix(A) or is_pydata_spmatrix(A)):
        A = np.asarray(A)

    n, m = A.shape

    if k <= 0 or k >= min(n, m):
        raise ValueError("k must be between 1 and min(A.shape), k=%d" % k)

    if isinstance(A, LinearOperator):
        if n > m:
            X_dot = A.matvec
            X_matmat = A.matmat
            XH_dot = A.rmatvec
            XH_mat = A.rmatmat
        else:
            X_dot = A.rmatvec
            X_matmat = A.rmatmat
            XH_dot = A.matvec
            XH_mat = A.matmat

            dtype = getattr(A, 'dtype', None)
            if dtype is None:
                dtype = A.dot(np.zeros([m, 1])).dtype

    else:
        if n > m:
            X_dot = X_matmat = A.dot
            XH_dot = XH_mat = _herm(A).dot
        else:
            XH_dot = XH_mat = A.dot
            X_dot = X_matmat = _herm(A).dot

    def matvec_XH_X(x):
        return XH_dot(X_dot(x))

    def matmat_XH_X(x):
        return XH_mat(X_matmat(x))

    XH_X = LinearOperator(matvec=matvec_XH_X, dtype=A.dtype,
                          matmat=matmat_XH_X,
                          shape=(min(A.shape), min(A.shape)))

    # Get a low rank approximation of the implicitly defined gramian matrix.
    # This is not a stable way to approach the problem.
    if solver == 'lobpcg':

        if k == 1 and v0 is not None:
            X = np.reshape(v0, (-1, 1))
        else:
            X = np.random.RandomState(52).randn(min(A.shape), k)

        eigvals, eigvec = lobpcg(XH_X, X, tol=tol ** 2, maxiter=maxiter,
                                 largest=largest)

    elif solver == 'arpack' or solver is None:
        eigvals, eigvec = eigsh(XH_X, k=k, tol=tol ** 2, maxiter=maxiter,
                                ncv=ncv, which=which, v0=v0)

    else:
        raise ValueError("solver must be either 'arpack', or 'lobpcg'.")

    # Gramian matrices have real non-negative eigenvalues.
    eigvals = np.maximum(eigvals.real, 0)

    # Use the sophisticated detection of small eigenvalues from pinvh.
    t = eigvec.dtype.char.lower()
    factor = {'f': 1E3, 'd': 1E6}
    cond = factor[t] * np.finfo(t).eps
    cutoff = cond * np.max(eigvals)

    # Get a mask indicating which eigenpairs are not degenerately tiny,
    # and create the re-ordered array of thresholded singular values.
    above_cutoff = (eigvals > cutoff)
    nlarge = above_cutoff.sum()
    nsmall = k - nlarge
    slarge = np.sqrt(eigvals[above_cutoff])
    s = np.zeros_like(eigvals)
    s[:nlarge] = slarge
    if not return_singular_vectors:
        return np.sort(s)

    if n > m:
        vlarge = eigvec[:, above_cutoff]
        ularge = X_matmat(vlarge) / slarge if return_singular_vectors != 'vh' else None
        vhlarge = _herm(vlarge)
    else:
        ularge = eigvec[:, above_cutoff]
        vhlarge = _herm(X_matmat(ularge) / slarge) if return_singular_vectors != 'u' else None

    u = _augmented_orthonormal_cols(ularge, nsmall) if ularge is not None else None
    vh = _augmented_orthonormal_rows(vhlarge, nsmall) if vhlarge is not None else None

    indexes_sorted = np.argsort(s)
    s = s[indexes_sorted]
    if u is not None:
        u = u[:, indexes_sorted]
    if vh is not None:
        vh = vh[indexes_sorted]

    return u, s, vh
