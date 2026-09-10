#!/usr/bin/env python3
"""Package only the current runner's bound and verified runtime."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import release_pipeline as pipeline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    context = pipeline.read(args.work / 'context.json')
    candidate = pipeline.read(args.work / 'candidate.json')
    evidence = pipeline.read(args.result / 'evidence.json')
    target = evidence['binding']['target']
    if target not in context['targets'] or evidence['binding']['releaseId'] != context['releaseId'] + '-' + target or evidence['binding']['sequence'] != context['sequence']:
        raise ValueError('Runner evidence differs from requested release')
    record = pipeline.package(args.result / 'package', candidate,
                              pipeline.read(args.result / 'patch-proof.json'), evidence,
                              args.result, context['baseUrl'])
    pipeline.write(args.result / 'release.json', record)


if __name__ == '__main__':
    main()
