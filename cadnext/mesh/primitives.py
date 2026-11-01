"""Geometric distances with minimal local axis coordinates and deterministic starts."""

import numpy as np
from scipy.optimize import least_squares

KINDS = ("plane", "cylinder", "cone", "sphere", "torus")


def basis(axis):
    axis = np.asarray(axis, dtype=float)
    axis /= np.linalg.norm(axis)
    x = np.cross(axis, np.eye(3)[np.argmin(np.abs(axis))])
    x /= np.linalg.norm(x)
    return np.column_stack((x, np.cross(axis, x)))


def span(xy):
    angles = np.sort(np.mod(np.arctan2(xy[:, 1], xy[:, 0]), 2*np.pi))
    return float(np.rad2deg(2*np.pi-np.diff(np.r_[angles, angles[0]+2*np.pi]).max()))


class Primitive:
    def __init__(self, kind, origin, scale, axis):
        self.kind, self.origin, self.scale = kind, origin, scale
        self.axis = np.asarray(axis)
        self.basis = basis(axis)

    def decode(self, p):
        if self.kind == "sphere":
            return p[:3], None, p[3:]
        axis = self.axis+self.basis @ p[-2:]
        axis /= np.linalg.norm(axis)
        if self.kind == "plane":
            return axis*p[0], axis, []
        if self.kind == "cylinder":
            center = self.basis @ p[:2]
            center -= axis*np.dot(center, axis)
            return center, axis, p[2:3]
        return p[:3], axis, p[3:-2]

    def residual(self, p, points):
        center, axis, sizes = self.decode(p)
        delta = points-center
        if self.kind == "sphere":
            return np.linalg.norm(delta, axis=1)-sizes[0]
        z = delta @ axis
        if self.kind == "plane":
            return z
        rho = np.linalg.norm(delta-z[:, None]*axis, axis=1)
        if self.kind == "cylinder":
            return rho-sizes[0]
        if self.kind == "cone":
            return (rho-z*sizes[0])/np.sqrt(1+sizes[0]**2)
        return np.hypot(rho-sizes[0], z)-sizes[1]

    def distance(self, points):
        return self.residual(self.p, (points-self.origin)/self.scale)*self.scale

    def parameters(self):
        center, axis, sizes = self.decode(self.p)
        result = {"center_mm" if self.kind in {"sphere", "torus"} else "origin_mm":
                  (self.origin+center*self.scale).tolist()}
        if axis is not None:
            result["normal" if self.kind == "plane" else "axis"] = axis.tolist()
        if self.kind in {"sphere", "cylinder"}:
            result["radius_mm"] = float(abs(sizes[0])*self.scale)
        elif self.kind == "cone":
            result["half_angle_degrees"] = float(np.rad2deg(np.arctan(sizes[0])))
        elif self.kind == "torus":
            result.update(major_radius_mm=float(abs(sizes[0])*self.scale),
                          minor_radius_mm=float(abs(sizes[1])*self.scale))
        return result


def _start(kind, points, normals, axis, origin, scale):
    model = Primitive(kind, origin, scale, axis)
    if kind == "plane":
        initial = [float(np.median(points @ axis)), 0, 0]
    elif kind == "sphere":
        a = np.column_stack((2*points, np.ones(len(points))))
        c = np.linalg.lstsq(a, np.sum(points**2, axis=1), rcond=None)[0][:3]
        initial = [*c, np.median(np.linalg.norm(points-c, axis=1))]
    elif kind == "cylinder":
        xy = points @ model.basis
        c = np.linalg.lstsq(np.column_stack((2*xy, np.ones(len(xy)))), np.sum(xy**2, axis=1), rcond=None)[0][:2]
        initial = [*c, np.median(np.linalg.norm(xy-c, axis=1)), 0, 0]
    elif kind == "cone":
        # Cone normal planes meet at its apex, independently of tessellation density.
        apex = np.linalg.lstsq(normals, np.einsum("ij,ij->i", normals, points), rcond=None)[0]
        z = (points-apex) @ axis
        if np.median(z) < 0:
            model = Primitive(kind, origin, scale, -axis)
            axis, z = -axis, -z
        rho = np.linalg.norm((points-apex)-z[:, None]*axis, axis=1)
        initial = [*apex, max(float(np.median(rho/np.maximum(z, 1e-8))), 1e-5), 0, 0]
    else:
        center = np.median(points, axis=0)
        z = (points-center) @ axis
        rho = np.linalg.norm(points-center-z[:, None]*axis, axis=1)
        radius = np.median(rho)
        initial = [*center, radius, max(float(np.median(np.hypot(rho-radius, z))), 1e-5), 0, 0]
    return model, np.asarray(initial)


def fit_primitive(points, normals, kind, tolerance):
    origin = np.median(points, axis=0)
    scale = max(float(np.ptp(points, axis=0).max()), 1e-8)
    q = (points-origin)/scale
    # Bound optimizer work; final residuals and facet checks always use all input.
    sample = np.linspace(0, len(q)-1, min(len(q), 1500), dtype=int)
    q, n = q[sample], normals[sample]
    _, vectors = np.linalg.eigh(np.cov(n.T))
    _, pca = np.linalg.eigh(np.cov(q.T))
    axes = [vectors[:, 0], pca[:, 0], pca[:, -1]]
    if kind == "plane":
        axes = [pca[:, 0]]
    if kind == "sphere":
        axes = [pca[:, 0]]
    rng = np.random.default_rng(73421)
    candidates = []
    for axis in axes:
        # Least median selects a deterministic initial hypothesis before geometric refinement.
        for ids in [np.arange(len(q)), *[rng.choice(len(q), min(24, len(q)), replace=False) for _ in range(8)]]:
            try:
                model, initial = _start(kind, q[ids], n[ids], axis, origin, scale)
                score = np.median(model.residual(initial, q)**2)
                if np.isfinite(score):
                    candidates.append((float(score), model, initial))
            except np.linalg.LinAlgError:
                pass
    best = None
    for _, model, initial in sorted(candidates, key=lambda c: c[0])[:3]:
        solution = least_squares(lambda p: model.residual(p, q), initial, loss="soft_l1",
                                 f_scale=tolerance/scale/10, max_nfev=160)
        score = float(np.median(model.residual(solution.x, q)**2))
        if best is None or score < best[0]:
            best = score, model, solution
    _, model, solution = best
    model.p = solution.x
    model.converged = solution.success
    # The residual Jacobian is normalized in length and has no redundant axis coordinate.
    jac = solution.jac
    singular = np.linalg.svd(jac, compute_uv=False)
    model.condition = float(singular[0]/max(singular[-1], 1e-30))
    return model
