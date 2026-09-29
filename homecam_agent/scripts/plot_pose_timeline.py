#!/usr/bin/env python3
"""Plot real interval samples, never reconstruct a timeline from averages."""
import argparse
import json
from pathlib import Path
import statistics


def load_condition(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest['mode'] != 'realtime':
        raise ValueError('a timeline requires realtime replay measurements')
    rows = json.loads((directory / 'summary.json').read_text())
    points, boundaries, inference = [], [], []
    elapsed = 0.0
    for row in rows:
        path = directory / f"{row['case_id']}-{row['repeat']}.json"
        detail = json.loads(path.read_text())
        samples = detail.get('resource_samples', [])
        if not samples or row['errors']:
            raise ValueError(f'missing interval measurements or replay errors: {path.name}')
        previous = 0.0
        for point in samples:
            if not previous < point['elapsed_s'] <= row['wall_s']:
                raise ValueError('sample timestamp outside replay or out of order')
            previous = point['elapsed_s']
            points.append({**point, 'replay_elapsed_s': point['elapsed_s'],
                           'elapsed_s': elapsed + point['elapsed_s'],
                           'case_id': row['case_id'], 'repeat': row['repeat']})
        inference.extend(detail['raw']['inference_ms'])
        elapsed += row['wall_s']
        boundaries.append(elapsed)
    return {
        'manifest': manifest,
        'replay_order': [(row['case_id'], row['repeat']) for row in rows],
        'points': points,
        'boundaries': boundaries,
        'wall_s': elapsed,
        'cpu_median': statistics.median(row['cpu_percent'] for row in rows),
        'inference_ms_median': statistics.median(inference),
        'processed_fps_median': statistics.median(row['processed_fps'] for row in rows),
        'inference_count': sum(row['inference_count'] for row in rows),
        'conversion_count': sum(row['conversion_count'] for row in rows),
        'dropped_frames': sum(row['dropped_before_callback'] for row in rows),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--font', type=Path,
                        default=Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'))
    args = parser.parse_args()
    if args.output.exists():
        parser.error('use a new output directory')
    before, after = load_condition(args.before), load_condition(args.after)
    for field in ('model_sha256', 'media_sha256', 'camera_input', 'cases', 'repeats'):
        if before['manifest'][field] != after['manifest'][field]:
            raise ValueError(f'comparison inputs differ: {field}')
    if before['replay_order'] != after['replay_order']:
        raise ValueError('replay order differs')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    font = FontProperties(fname=str(args.font))
    plt.rcParams.update({
        'font.family': font.get_name(), 'font.size': 11, 'axes.unicode_minus': False,
        'figure.facecolor': '#1c2731', 'axes.facecolor': '#1c2731',
        'text.color': '#eff5fa', 'axes.labelcolor': '#b8c9d9',
        'xtick.color': '#b8c9d9', 'ytick.color': '#b8c9d9',
        'axes.edgecolor': '#435568', 'savefig.facecolor': '#1c2731',
    })
    fig, axes = plt.subplots(2, 1, figsize=(15, 9), sharex=True,
                             gridspec_kw={'height_ratios': [1.6, 1]})
    fig.subplots_adjust(left=.075, right=.97, top=.79, bottom=.20, hspace=.19)
    reduction = 100 * (1 - after['cpu_median'] / before['cpu_median'])
    fig.text(.06, .95, '로컬 Pose · 시간별 CPU / GPU 사용률', fontsize=23, weight='bold')
    fig.text(.06, .902,
             f"i5-12400F / RTX 3070 · 같은 영상 {len(before['manifest']['cases'])}개 × "
             f"{before['manifest']['repeats']}회 · 전후 순차 실행을 같은 재생 시간축에 표시",
             fontsize=12, color='#b8c9d9')
    fig.text(.06, .847,
             f"CPU 재생별 평균의 중앙값: {before['cpu_median']:.1f}% → "
             f"{after['cpu_median']:.1f}%  ({reduction:.1f}% 감소)",
             fontsize=16, color='#94e4b1', weight='bold')
    for ax in axes:
        ax.set_axisbelow(True)
        ax.grid(axis='y', color='#9aaabd', alpha=.18)
        ax.spines[['top', 'right']].set_visible(False)
        ax.set_ylim(bottom=0)
        for boundary in before['boundaries'][:-1]:
            ax.axvline(boundary, color='#9aaabd', alpha=.12, linewidth=.7)
    for label, color, condition in (
        ('개선 전 · CPU 추론', '#aaa0ff', before),
        ('개선 후 · 변환 제한 + 스레드 조정 + GPU 추론', '#94e4b1', after),
    ):
        times = [point['elapsed_s'] for point in condition['points']]
        axes[0].plot(times, [point['cpu_percent'] for point in condition['points']],
                     color=color, marker='.', markersize=4, linewidth=1.3, label=label)
        axes[1].plot(times, [point['gpu_percent'] for point in condition['points']],
                     color=color, marker='.', markersize=4, linewidth=1.1, label=label)
    axes[0].set_ylabel('Pose 프로세스 CPU (%)\n코어 하나 = 100%')
    peak_cpu = max(point['cpu_percent'] for condition in (before, after)
                   for point in condition['points'])
    axes[0].set_ylim(0, max(100, peak_cpu * 1.18))
    axes[1].set_ylabel('GPU 전체 사용률 (%)\n다른 프로그램 포함')
    axes[1].set_ylim(0, 100)
    axes[1].set_xlabel('측정 구간 누적 시간 (초) · 모델 준비 / 영상 사이 공백 제외')
    axes[0].legend(loc='upper right', frameon=True, facecolor='#1c2731', edgecolor='#435568')
    fig.text(.06, .125,
             f"추론 중앙값: {before['inference_ms_median']:.2f} → {after['inference_ms_median']:.2f}ms"
             f"   |   처리 fps: {before['processed_fps_median']:.2f} → {after['processed_fps_median']:.2f}"
             f"   |   추론 횟수: {before['inference_count']} → {after['inference_count']}",
             fontsize=12)
    fig.text(.06, .078,
             '약 0.5초 간격 실제 측정. 점 사이 선은 연결 표시이며 두 조건을 동시에 실행한 기록이 아님.',
             fontsize=10, color='#b8c9d9')
    fig.text(.06, .044,
             '브라우저·원격 데스크톱 등 배경 부하가 있음. GPU 전체 값으로 Pose 단독 부하나 전력 절감률을 판단하지 않음. Jetson 미측정.',
             fontsize=10, color='#b8c9d9')
    args.output.mkdir(parents=True, mode=0o700)
    fig.savefig(args.output / 'pose-cpu-gpu-timeline.png', dpi=150)
    svg = args.output / 'pose-cpu-gpu-timeline.svg'
    fig.savefig(svg)
    # Matplotlib adds trailing blanks to SVG paths; keep generated diffs clean.
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines()) + '\n')
    report = {'before': before, 'after': after,
              'cpu_reduction_percent': reduction,
              'scope': 'separate local callback replays, aligned elapsed time, not Jetson or all ROS nodes'}
    (args.output / 'timeline-data.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: {field: v[field] for field in (
        'cpu_median', 'inference_ms_median', 'processed_fps_median', 'inference_count',
        'conversion_count', 'dropped_frames')} for k, v in [('before', before), ('after', after)]},
        indent=2))


if __name__ == '__main__':
    main()
