"""Public development evaluator, NOT a secure hidden-test sandbox.

Usage: python evaluator/dev_eval.py --task chunking|integration
       --candidate tasks/<task>/candidate.py
Only standard-library Python is required. All fixtures here are PUBLIC.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import selectors
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def chunk_cases():
    """Known-answer construction, independent of a Markdown parser."""
    cases = []
    for variant in range(8):
        parts, spans = [], []
        def add(text, section=None):
            start = len(''.join(parts).encode())
            parts.append(text)
            if section:
                spans.append((start, start + len(text.encode()), section))
        if variant == 1:
            add('---\ntitle: sample\n---\n')
        add('# Demonstration paper\n')
        add('## Method\n')
        add('A repeatable scientific observation.\n\n', 'Method')
        if variant == 2:
            add('```python\n## not a heading\nx = 1\n```\n', 'Method')
        if variant == 3:
            add('## Method\n')
            add('Another method with a different body.\n', 'Method')
        if variant == 4:
            add('## Learned Preferences\n')
            add('Preference learning is substantive research.\n', 'Learned Preferences')
        if variant == 5:
            add('推理与证明保持原始文字。' * 60 + '\n', 'Method')
        if variant == 6:
            add('long scientific paragraph ' * 80 + '\n', 'Method')
        add('### Results\n' if variant == 7 else '## Results\n')
        add('The result is reproducible and has limitations.\n', 'Results')
        add('## References\n')
        add('An excluded citation, not experimental evidence.\n')
        cases.append(dict(document=''.join(parts), max_bytes=160, spans=spans))
    return cases


def grade_chunks(case, output):
    raw = case['document'].encode()
    expected = {}
    for start, end, section in case['spans']:
        for i in range(start, end):
            if raw[i:i+1] not in b' \t\r\n':
                expected[i] = section
    if not isinstance(output, list) or len(output) > 10000:
        return False
    seen, identifiers = set(), set()
    for c in output:
        if not isinstance(c, dict) or set(c) != {'id', 'section', 'start', 'end'}:
            return False
        a, b = c['start'], c['end']
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(raw):
            return False
        if b - a > case['max_bytes'] or not isinstance(c['id'], str) or not c['id']:
            return False
        if c['id'] in identifiers:
            return False
        identifiers.add(c['id'])
        try:
            raw[a:b].decode('utf-8')
        except UnicodeDecodeError:
            return False
        for i in range(a, b):
            if raw[i:i+1] in b' \t\r\n':
                continue
            if i in seen or expected.get(i) != c['section']:
                return False
            seen.add(i)
    return seen == set(expected)


def field(parameters, t, y):
    a, b, omega, phase = parameters
    return [-a * v + b * math.sin(omega * t + phase + j * .23)
            + .08 * math.tanh(y[(j+1) % len(y)]) for j, v in enumerate(y)]


def reference(parameters, initial, n=4096):
    y = initial[:]
    path = []
    h = 1. / n
    for i in range(n):
        t = i * h
        k1 = field(parameters, t, y)
        k2 = field(parameters, t+h/2, [v+h*d/2 for v,d in zip(y,k1)])
        k3 = field(parameters, t+h/2, [v+h*d/2 for v,d in zip(y,k2)])
        k4 = field(parameters, t+h, [v+h*d for v,d in zip(y,k3)])
        y = [v+h*(a+2*b+2*c+d)/6 for v,a,b,c,d in zip(y,k1,k2,k3,k4)]
        if (i + 1) % (n // 4) == 0:
            path.append(y[:])
    return path


def integration_cases():
    rng = random.Random(20261012)  # Public development only; never a hidden seed.
    return [(tuple(rng.uniform(*bounds) for bounds in ((.2,2.),(.1,.9),(1.,8.),(0.,6.))),
             [rng.uniform(-1.,1.) for _ in range(dim)]) for dim in (2,4,8) for _ in range(3)]


def read_line(process, selector, deadline):
    if not selector.select(max(0., deadline-time.monotonic())):
        raise TimeoutError('candidate timeout')
    line = process.stdout.readline(65537)
    if not line or len(line) > 65536:
        raise ValueError('missing or oversized RPC message')
    return json.loads(line)


def integration_run(candidate, parameters, initial):
    target = reference(parameters, initial)
    process = subprocess.Popen([sys.executable, '-u', str(candidate)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                               bufsize=1)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    calls, error, valid = 0, None, False
    deadline = time.monotonic() + 10
    try:
        process.stdin.write(json.dumps(dict(y0=initial, baseline_steps=256, tolerance=.01))+'\n')
        process.stdin.flush()
        while True:
            request = read_line(process, selector, deadline)
            y = request.get('y')
            if not isinstance(y, list) or len(y) != len(initial) or not all(
                    type(v) in (float,int) and math.isfinite(v) and abs(v) < 1e6 for v in y):
                raise ValueError('invalid vector')
            if request.get('op') == 'finish':
                path = request.get('path')
                if not isinstance(path,list) or len(path) != 4 or any(
                        not isinstance(row,list) or len(row) != len(initial) or any(
                            type(v) not in (float,int) or not math.isfinite(v) for v in row)
                        for row in path) or path[-1] != y:
                    raise ValueError('invalid checkpoint path')
                error = max(abs(v-w) for row,truth in zip(path,target) for v,w in zip(row,truth))
                valid = error <= .01
                process.stdin.write('{}\n')
                process.stdin.flush()
                break
            if request.get('op') != 'field' or calls >= 512:
                raise ValueError('invalid request or field budget exhausted')
            t = request.get('t')
            if type(t) not in (float,int) or not math.isfinite(t) or not 0 <= t <= 1:
                raise ValueError('invalid time')
            calls += 1
            value = field(parameters, t, y)
            process.stdin.write(json.dumps(dict(value=value))+'\n')
            process.stdin.flush()
    except (ValueError, TypeError, AttributeError, TimeoutError, BrokenPipeError, json.JSONDecodeError):
        valid = False
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait()
    # Zero-query answers do not qualify as numerical solver improvements.
    valid = valid and calls > 0
    return dict(valid=valid, calls=calls, path_linf=error,
                reward=min(4.,256/max(1,calls)) if valid else 0.)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', choices=('chunking','integration'), required=True)
    parser.add_argument('--candidate', type=Path)
    args = parser.parse_args()
    candidate = (args.candidate or ROOT/'tasks'/args.task/'candidate.py').resolve()
    if args.task == 'chunking':
        rows = []
        for case in chunk_cases():
            try:
                result = subprocess.run([sys.executable,str(candidate)], input=json.dumps(
                    {k:case[k] for k in ('document','max_bytes')}), text=True,
                    capture_output=True, timeout=5, check=True)
                valid = grade_chunks(case,json.loads(result.stdout))
            except (subprocess.SubprocessError, ValueError):
                valid = False
            rows.append(dict(valid=valid))
        score = sum(r['valid'] for r in rows)/len(rows)
    else:
        rows = [integration_run(candidate,p,y) for p,y in integration_cases()]
        score = sum(r['reward'] for r in rows)/len(rows) if all(r['valid'] for r in rows) else 0.
    print(json.dumps(dict(task=args.task,split='public-dev',cases=len(rows),score=score,
                         passed=sum(r['valid'] for r in rows),rows=rows,
                         candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest()),indent=2))


if __name__ == '__main__':
    main()
