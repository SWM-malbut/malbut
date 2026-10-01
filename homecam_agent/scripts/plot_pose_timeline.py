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


def validate_comparison(conditions):
    """Every stage must replay identical inputs with the same measurement code."""
    reference = conditions[0]
    for condition in conditions[1:]:
        for field in ('model_sha256', 'media_sha256', 'camera_input', 'cases', 'repeats',
                      'runner_sha256', 'ort', 'opencv'):
            if (field not in reference['manifest'] or field not in condition['manifest']
                    or reference['manifest'][field] != condition['manifest'][field]):
                raise ValueError(f'comparison inputs differ: {field}')
        if reference['replay_order'] != condition['replay_order']:
            raise ValueError('replay order differs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--conversion-only', type=Path,
                        help='intermediate stage: early conversion gate, default CPU threads')
    parser.add_argument('--cpu-tuned', type=Path,
                        help='intermediate stage: early gate plus CPU thread/spinning tuning')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--font', type=Path,
                        default=Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'))
    args = parser.parse_args()
    if args.output.exists():
        parser.error('use a new output directory')
    if (args.conversion_only is None) != (args.cpu_tuned is None):
        parser.error('provide both intermediate stages or neither')
    before, after = load_condition(args.before), load_condition(args.after)
    staged = args.conversion_only is not None
    series = [('before', '0. 기존 CPU', '#aaa0ff', '-', before)]
    if staged:
        series.extend([
            ('conversion_only', '1. 변환 전 5fps 제한만', '#f5c97d', '--',
             load_condition(args.conversion_only)),
            ('cpu_tuned', '2. 1단계 + 스레드 조정', '#7bd0e4', '-.',
             load_condition(args.cpu_tuned)),
        ])
    series.append(('after', '3. 2단계 + GPU 추론' if staged else '전체 개선 + GPU 추론',
                   '#94e4b1', '-', after))
    validate_comparison([condition for _, _, _, _, condition in series])
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
    fig, axes = plt.subplots(2, 1, figsize=(15, 12), sharex=True,
                             gridspec_kw={'height_ratios': [1.6, 1]})
    fig.subplots_adjust(left=.075, right=.97, top=.795, bottom=.36, hspace=.22)
    reduction = 100 * (1 - after['cpu_median'] / before['cpu_median'])
    fig.text(.06, .956, '로컬 Pose · 단계별 CPU / GPU 사용률' if staged
             else '로컬 Pose · 시간별 CPU / GPU 사용률', fontsize=23, weight='bold')
    fig.text(.06, .918,
             f"i5-12400F / RTX 3070 · 같은 영상 {len(before['manifest']['cases'])}개 × "
             f"{before['manifest']['repeats']}회 · 각 조건을 따로 실행한 뒤 같은 재생 시간축에 표시",
             fontsize=12, color='#b8c9d9')
    fig.text(.06, .881, '변경을 하나씩 누적: 기존 → 변환 제한 → 스레드 조정 → GPU 추론'
             if staged else f'전체 변경 후 CPU 사용량 {reduction:.1f}% 감소',
             fontsize=14, color='#d4e1eb')
    for ax in axes:
        ax.set_axisbelow(True)
        ax.grid(axis='y', color='#9aaabd', alpha=.18)
        ax.spines[['top', 'right']].set_visible(False)
        ax.set_ylim(bottom=0)
        for boundary in before['boundaries'][:-1]:
            ax.axvline(boundary, color='#9aaabd', alpha=.12, linewidth=.7)
    for _, label, color, style, condition in series:
        times = [point['elapsed_s'] for point in condition['points']]
        axes[0].plot(times, [point['cpu_percent'] for point in condition['points']],
                     color=color, linestyle=style, marker='.', markersize=4,
                     linewidth=1.3, label=label)
        axes[1].plot(times, [point['gpu_percent'] for point in condition['points']],
                     color=color, linestyle=style, marker='.', markersize=4,
                     linewidth=1.1, label=label)
    axes[0].set_ylabel('Pose 프로세스 CPU (%)\n코어 하나 = 100%')
    peak_cpu = max(point['cpu_percent'] for _, _, _, _, condition in series
                   for point in condition['points'])
    axes[0].set_ylim(0, max(100, peak_cpu * 1.18))
    axes[1].set_ylabel('GPU 전체 사용률 (%)\n다른 프로그램 포함')
    axes[1].set_ylim(0, 100)
    axes[1].set_xlabel('측정 구간 누적 시간 (초) · 모델 준비 / 영상 사이 공백 제외')
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='lower left', bbox_to_anchor=(.06, .818),
               ncol=2, frameon=False, fontsize=12)
    table_ax = fig.add_axes([.06, .115, .91, .175])
    table_ax.axis('off')
    table = table_ax.table(
        cellText=[[label, f"{condition['cpu_median']:.1f}%",
                   f"{condition['inference_ms_median']:.2f}ms",
                   f"{condition['processed_fps_median']:.2f}",
                   f"{condition['conversion_count']} / {condition['inference_count']}",
                   str(condition['dropped_frames'])]
                  for _, label, _, _, condition in series],
        colLabels=['실행 조건', 'CPU 중앙값¹', '추론 중앙값', '실제 fps', '변환 / 추론 횟수', '입력 건너뜀²'],
        colWidths=[.31, .14, .16, .10, .17, .12], cellLoc='center', bbox=[0, 0, 1, 1])
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    for (row, _), cell in table.get_celld().items():
        cell.set_edgecolor('#435568')
        cell.set_facecolor('#293845' if row == 0 else '#1c2731')
        cell.get_text().set_color('#eff5fa' if row == 0 else series[row - 1][2])
    fig.text(.06, .077,
             f"¹ 재생별 평균 {len(before['replay_order'])}개의 중앙값. "
             '스레드 조정: ORT 2스레드 · spinning OFF · OpenCV 1스레드.',
             fontsize=10, color='#b8c9d9')
    fig.text(.06, .048,
             '² 콜백이 늦으면 최신 입력을 고르는 재생 규칙의 건너뜀 수. 실제 DDS 손실 아님. 약 0.5초 간격 측정 · 준비 시간 제외.',
             fontsize=10, color='#b8c9d9')
    fig.text(.06, .021,
             'GPU 전체 값에는 다른 프로그램의 부하도 포함됨. Pose 단독 GPU 부하나 전체 전력 절감률로 해석하지 않음. Jetson 미측정.',
             fontsize=10, color='#b8c9d9')
    args.output.mkdir(parents=True, mode=0o700)
    fig.savefig(args.output / 'pose-cpu-gpu-timeline.png', dpi=150)
    svg = args.output / 'pose-cpu-gpu-timeline.svg'
    fig.savefig(svg)
    # Matplotlib adds trailing blanks to SVG paths; keep generated diffs clean.
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines()) + '\n')
    report = {'before': before, 'after': after,
              'conditions': {key: condition for key, _, _, _, condition in series},
              'comparison_mode': 'cumulative_stages' if staged else 'before_after',
              'cpu_reduction_percent': reduction,
              'scope': 'separate local callback replays, aligned elapsed time, not Jetson or all ROS nodes'}
    (args.output / 'timeline-data.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: {field: v[field] for field in (
        'cpu_median', 'inference_ms_median', 'processed_fps_median', 'inference_count',
        'conversion_count', 'dropped_frames')} for k, _, _, _, v in series},
        indent=2))


if __name__ == '__main__':
    main()
