"""Process orchestration for running fall rules alongside speech dialogue."""

import signal
import sys
import threading
import time
from typing import Any, Callable, Sequence


def spin_fall_rules(executor: Any, logger: Any, failed: threading.Event) -> None:
    """Spin the fall rules on their own thread, apart from dialogue waits."""
    from rclpy.executors import ExternalShutdownException

    try:
        executor.spin()
    except ExternalShutdownException:
        pass
    except Exception as error:
        # Like the former coordinator process exiting: heartbeats stop and the
        # fall runtime pauses itself; speech keeps working.
        failed.set()
        logger.error('fall_rules_failed: ' + type(error).__name__)


def run_with_fall_rules(
    args: Any,
    ros_args: Sequence[str],
    settings: Any,
    *,
    create_node_fn: Callable[..., Any],
    command_lines_cls: Any,
    read_commands_fn: Callable[..., Any],
) -> int:
    """Run the fall rules beside the dialogue so that either can fail alone."""
    try:
        import rclpy
        from rclpy.executors import (
            ExternalShutdownException,
            MultiThreadedExecutor,
            SingleThreadedExecutor,
        )
        from rclpy.signals import SignalHandlerOptions

        from malbut_agent_server.fall_rules.node import FallRulesNode
    except ImportError:
        print('Source ROS 2 and the built workspace first.', file=sys.stderr)
        return 2
    # As the fall coordinator did: keep ROS alive after a signal long enough to
    # cancel the fall mission these rules asked the Manager for.
    stopped = threading.Event()
    previous = {
        sig: signal.signal(sig, lambda *_: stopped.set())
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    failed = threading.Event()
    rules = rules_executor = rules_thread = None
    node = executor = None
    initialized = False
    from malbut_agent_server.confirmation_answers import ConfirmationAnswers
    confirmation_answers = ConfirmationAnswers()

    def report(text: str) -> None:
        owner = rules if rules is not None else node
        if owner is not None:
            owner.get_logger().error(text)
        else:
            print(text, file=sys.stderr)

    def drop_dialogue(error: Exception) -> None:
        # Like the Agent process exiting beside the former coordinator: the
        # Manager then finds no confirmation server and the question fails.
        nonlocal node, executor
        report('agent_communication_failed: ' + type(error).__name__)
        try:
            if node is not None:
                node.destroy_node()
        finally:
            if executor is not None:
                executor.shutdown(timeout_sec=0)
            node = executor = None

    def rules_running() -> bool:
        return rules_thread is not None and rules_thread.is_alive()

    try:
        rclpy.init(args=ros_args, signal_handler_options=SignalHandlerOptions.NO)
        initialized = True
        try:
            rules = FallRulesNode(
                runtime_id=args.fall_runtime_id,
                bridge_runtime_id=args.fall_bridge_runtime_id,
                vlm_runtime_id=args.fall_vlm_runtime_id,
                flow=args.fall_flow,
                approach_enabled=not args.no_fall_approach,
                confirmation_answers=confirmation_answers,
            )
            rules_executor = MultiThreadedExecutor(num_threads=2)
            rules_executor.add_node(rules)
        except Exception as error:
            # Like the coordinator failing at start: no heartbeat, so the fall
            # runtime pauses itself; speech still starts.
            failed.set()
            print('Fall rules failed to start: ' + type(error).__name__, file=sys.stderr)
            if rules is not None:
                rules.destroy_node()
            rules = rules_executor = None
        else:
            rules_thread = threading.Thread(
                target=spin_fall_rules,
                args=(rules_executor, rules.get_logger(), failed),
                name='fall-rules',
                daemon=True,
            )
            rules_thread.start()
            rules.get_logger().info('Fall rules started' + (
                '' if args.speech and settings is not None
                else '; speech is off, so the Agent asks no questions'
            ))
        if args.speech and settings is not None:
            try:
                node = create_node_fn(
                    speech_db_path=args.db_path,
                    goal_response_timeout_s=args.goal_response_timeout_s,
                    dialogue_settings=settings,
                    enable_manager_commands=args.enable_manager_commands,
                    prerecorded_audio=args.prerecorded_audio,
                    confirmation_answers=confirmation_answers,
                )
                executor = SingleThreadedExecutor()
                executor.add_node(node)
            except Exception as error:
                drop_dialogue(error)
            else:
                node.get_logger().info(
                    'Agent communication started; initializing speech dialogue worker. '
                    + ('Voice mission requests are routed through Manager.'
                       if args.enable_manager_commands
                       else 'Voice mission requests are disabled.'),
                )
        descriptor = sys.stdin.fileno() if node is not None else None
        lines = command_lines_cls()
        while rclpy.ok() and not stopped.is_set() and (node is not None or rules_running()):
            if node is None or executor is None:
                stopped.wait(0.05)
                continue
            try:
                executor.spin_once(timeout_sec=0.05)
                descriptor = read_commands_fn(node, descriptor, lines)
            except ExternalShutdownException:
                break
            except Exception as error:
                drop_dialogue(error)
                descriptor = None
    except ExternalShutdownException:
        pass
    except Exception as error:
        print('Agent failed: ' + type(error).__name__, file=sys.stderr)
        failed.set()
    finally:
        try:
            if rules is not None:
                pending = rules.close()
                deadline = time.monotonic() + 3.0
                while (pending is not None and not pending.done() and rules_running()
                       and time.monotonic() < deadline):
                    time.sleep(0.01)
        finally:
            try:
                if node is not None:
                    node.destroy_node()
            finally:
                if executor is not None:
                    executor.shutdown(timeout_sec=0)
                if rules_executor is not None:
                    rules_executor.shutdown(timeout_sec=3.0)
                if rules_thread is not None:
                    rules_thread.join(timeout=3.0)
                if rules is not None:
                    rules.destroy_node()
                if initialized and rclpy.ok():
                    rclpy.shutdown()
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    return 2 if failed.is_set() else 0
