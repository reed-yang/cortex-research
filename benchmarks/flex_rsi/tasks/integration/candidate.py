"""Original dense Euler starter using evaluator-owned field RPC.

Only this file is editable. Fields, reference answers and cost accounting
live in the parent evaluator, not in this process.
"""
import json
import sys


def send(value):
    print(json.dumps(value), flush=True)
    return json.loads(sys.stdin.readline())


def solve(problem):
    y = problem['y0']
    n = problem['baseline_steps']
    h = 1.0 / n
    path = []
    for i in range(n):
        derivative = send(dict(op='field', t=i * h, y=y))['value']
        y = [v + h * d for v, d in zip(y, derivative)]
        if (i + 1) % (n // 4) == 0:
            path.append(y[:])
    send(dict(op='finish', y=y, path=path))


if __name__ == '__main__':
    solve(json.loads(sys.stdin.readline()))
