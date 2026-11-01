"""Reproducible mesh-backend checks without connecting to SolidWorks."""

import argparse
import ctypes
import json
import math
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path


def peak_working_set():
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    *[(name, ctypes.c_size_t) for name in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counters.PeakWorkingSetSize)


def benchmark(face_count, queries):
    import numpy as np
    import trimesh

    started = time.perf_counter()
    n = max(1, math.isqrt(face_count // 2))
    x, y = np.meshgrid(np.linspace(0, 100, n+1), np.linspace(0, 100, n+1))
    vertices = np.column_stack((x.ravel(), y.ravel(), np.zeros(x.size)))
    a = (np.arange(n)[:, None] * (n+1) + np.arange(n)).ravel()
    faces = np.concatenate((np.column_stack((a, a+1, a+n+2)),
                            np.column_stack((a, a+n+2, a+n+1))))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    loaded_at = time.perf_counter()
    section = mesh.section(plane_origin=[50, 0, 0], plane_normal=[1, 0, 0])
    assert section is not None
    section_at = time.perf_counter()
    rng = np.random.default_rng(1729)
    points = np.column_stack((rng.uniform(1, 99, (queries, 2)), np.full(queries, 3.0)))
    _, distances, _ = mesh.nearest.on_surface(points)
    return {"target_triangles": face_count, "triangles": len(faces), "vertices": len(vertices),
            "queries": queries, "load_seconds": loaded_at-started,
            "section_seconds": section_at-loaded_at,
            "proximity_seconds": time.perf_counter()-section_at,
            "duration_seconds": time.perf_counter()-started,
            "max_distance_error_mm": float(np.max(np.abs(distances-3))),
            "peak_working_set_bytes": peak_working_set()}


def inspect_type_library(path):
    from cadnext.cad_reference import load_index
    from solidworks_mcp_server import com_call

    # Type information only, inside the existing STA implementation; no _app().
    index = com_call(load_index, path)
    prefixes = ("ITessellation.", "IBody2.GetTessellation", "IBody2.IGetTessellation")
    return {"version": index["version"], "symbols": {
        name: value for name, value in index["symbols"].items() if name.startswith(prefixes)}}


def main():
    parser = argparse.ArgumentParser(description="Проверить численную обработку сеток без CAD.")
    parser.add_argument("--sizes", nargs="+", type=int, default=[10_000, 100_000, 1_000_000])
    parser.add_argument("--queries", type=int, default=500)
    parser.add_argument("--child-face-count", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--type-library", type=Path)
    args = parser.parse_args()
    if not 1 <= args.queries <= 50_000 or any(not 2 <= size <= 1_000_000 for size in args.sizes):
        parser.error("Размер сетки: 2–1000000 граней; число запросов: 1–50000.")
    if args.type_library:
        result = inspect_type_library(args.type_library)
    elif args.child_face_count is not None:
        if not 2 <= args.child_face_count <= 1_000_000:
            parser.error("Размер сетки: 2–1000000 граней.")
        result = benchmark(args.child_face_count, args.queries)
    else:
        rows = []
        for size in args.sizes:
            env = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
            child = subprocess.run(
                [sys.executable, "-m", "scripts.mesh_feasibility", "--child-face-count", str(size),
                 "--queries", str(args.queries)], capture_output=True, text=True, timeout=120, env=env,
            )
            if child.returncode:
                raise RuntimeError(f"Проверка сетки {size} не завершена: {child.stderr[-2000:]}")
            rows.append(json.loads(child.stdout))
        result = {"benchmarks": rows, "cad_accessed": False}
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
