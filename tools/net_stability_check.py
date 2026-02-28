#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络连接稳定性测试工具

安全性增强：
- 验证所有输入参数，防止命令注入
- 改进线程管理和资源清理
- 增强异常处理和错误恢复机制
- 支持Python 3.6+

参考：
- Python subprocess官方文档（防止命令注入）
- Python threading官方最佳实践
"""

import subprocess
import re
import time
import sys
import threading
import argparse
import traceback
from datetime import datetime
from collections import defaultdict
import socket
import ipaddress

# Configuration parameters
DEFAULT_TEST_DURATION = 7200  # Default test duration in seconds (2 hours)
PING_INTERVAL = 1  # Ping interval in seconds
PING_TIMEOUT = 2  # Ping timeout in seconds
PING_COUNT = 3  # Number of ping packets per test
TCP_TIMEOUT = 2  # TCP connection timeout in seconds

# All nodes to test
NODE_IPS = [
    '11.2.26.250', '11.2.26.251', '11.2.26.252',
    '11.2.26.1', '11.2.26.2', '11.2.26.3', '11.2.26.4',
    '11.2.26.5', '11.2.26.7', '11.2.26.8', '11.2.26.9',
    '11.2.26.33', '11.2.26.34', '11.2.26.35', '11.2.26.36', '11.2.26.37',
    '11.2.26.40',
    '11.2.26.50',
    '11.2.26.57'
]


class NetworkTester(object):
    def __init__(self):
        self.results = defaultdict(list)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.active_threads = []  # 跟踪活动线程，确保正确清理

    def _validate_host(self, host):
        """
        验证主机名或IP地址，防止命令注入
        基于Python subprocess官方建议：验证所有用户输入
        """
        if not host or not isinstance(host, str):
            raise ValueError("Host must be a non-empty string")
        
        # 如果传入的是bytes，转换为str
        if isinstance(host, bytes):
            host = host.decode('utf-8')
        
        # 检查是否包含危险的shell字符
        dangerous_chars = [';', '&', '|', '`', '$', '(', ')', '<', '>', '\n', '\r', ' ']
        for char in dangerous_chars:
            if char in host:
                raise ValueError("Host contains dangerous character: %s" % char)
        
        # 验证IP地址或主机名格式
        try:
            # 尝试解析为IP地址
            ipaddress.ip_address(host)
        except (ValueError, AttributeError):
            # 如果不是IP，验证为主机名格式
            if not re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$', host):
                raise ValueError("Invalid host format: %s" % host)
        
        return host

    def run_ping(self, host):
        """
        Execute ping command and return output
        使用参数验证防止命令注入（Python subprocess官方最佳实践）
        """
        try:
            # 验证主机参数
            host = self._validate_host(host)
            
            # 使用列表传递参数，避免shell注入（Python官方推荐）
            cmd = ['ping', '-c', str(PING_COUNT), '-W', str(PING_TIMEOUT), '-n', '-q', host]
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=PING_TIMEOUT * PING_COUNT + 5)
            return output
        except subprocess.CalledProcessError as e:
            return e.output if hasattr(e, 'output') else ''
        except subprocess.TimeoutExpired:
            return ''  # 超时返回空字符串
        except ValueError as e:
            # 参数验证失败
            return ''
        except Exception as e:
            # 其他异常，记录但不中断
            return ''

    def parse_ping(self, output):
        """Parse ping command output"""
        packet_loss = 100
        rtts = []

        # Match packet loss
        loss_match = re.search(r'(\d+)% packet loss', output)
        if loss_match:
            packet_loss = float(loss_match.group(1))

        # Match RTT statistics
        rtt_match = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms', output)
        if rtt_match:
            rtts = map(float, [rtt_match.group(1), rtt_match.group(2), rtt_match.group(3)])

        return packet_loss, rtts

    def test_tcp_port(self, target):
        """
        Test TCP port connectivity
        增强参数验证和异常处理
        """
        # target 格式应该是 "host:port"，因为只有包含冒号的目标才会调用这个方法
        if ':' not in target:
            # 这确实不应该发生，为了安全起见抛出错误
            raise ValueError("TCP test target must be in format 'host:port', got: %s" % target)

        host, port_str = target.split(':', 1)
        
        # 验证主机
        try:
            host = self._validate_host(host)
        except ValueError as e:
            return False, 0
        
        # 验证端口
        try:
            port = int(port_str)
            if not (1 <= port <= 65535):
                raise ValueError("Port out of range: %d" % port)
        except (ValueError, TypeError):
            return False, 0

        start_time = time.time()
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(TCP_TIMEOUT)
            s.connect((host, port))
            # 明确类型：latency为float（毫秒）
            latency = float((time.time() - start_time) * 1000.0)  # Convert to milliseconds
            return True, latency
        except socket.timeout:
            return False, 0
        except socket.error:
            return False, 0
        except Exception:
            return False, 0
        finally:
            # 确保socket正确关闭
            if s:
                try:
                    s.close()
                except Exception:
                    pass

    def ping_worker(self, host):
        """
        Worker thread for ping testing
        增强异常处理和资源清理
        """
        try:
            # 验证主机参数
            host = self._validate_host(host)
        except ValueError as e:
            print("[%s] Invalid host %s: %s" % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), host, str(e)))
            return
        
        while not self.stop_event.is_set():
            try:
                start_time = time.time()

                output = self.run_ping(host)
                loss, rtt = self.parse_ping(output)

                with self.lock:
                    if loss == 100:
                        self.results[host].append({
                            'loss': 100,
                            'min': None,
                            'avg': None,
                            'max': None,
                            'type': 'icmp',
                            'timestamp': datetime.now().isoformat()
                        })
                        print("[%s] %s: ICMP Timeout" % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), host))
                    else:
                        # 确保rtt是列表且长度足够
                        if rtt and len(rtt) >= 3:
                            self.results[host].append({
                                'loss': loss,
                                'min': rtt[0],
                                'avg': rtt[1],
                                'max': rtt[2],
                                'type': 'icmp',
                                'timestamp': datetime.now().isoformat()
                            })
                            print("[%s] %s: ICMP Latency %.2fms (min: %.2fms, max: %.2fms), Loss %.1f%%" % (
                                datetime.now().strftime('%Y-%m-%d %H:%M:%S'), host, rtt[1], rtt[0], rtt[2], loss))
                        else:
                            # RTT解析失败，记录为超时
                            self.results[host].append({
                                'loss': 100,
                                'min': None,
                                'avg': None,
                                'max': None,
                                'type': 'icmp',
                                'timestamp': datetime.now().isoformat()
                            })
                            print("[%s] %s: ICMP Parse Error" % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), host))

                # Calculate remaining time and wait
                elapsed = float(time.time() - start_time)
                remaining = max(0.0, float(PING_INTERVAL) - elapsed)
                time.sleep(remaining)
            except Exception as e:
                # 捕获所有异常，防止线程崩溃
                print("[%s] Error in ping_worker for %s: %s" % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), host, str(e)))
                # 等待一段时间后继续
                time.sleep(PING_INTERVAL)

    def tcp_worker(self, target):
        """
        Worker thread for TCP testing
        增强异常处理和资源清理
        """
        # target 应该是 "host:port" 格式
        while not self.stop_event.is_set():
            try:
                start_time = time.time()

                success, latency = self.test_tcp_port(target)

                with self.lock:
                    self.results[target].append({
                        'success': success,
                        'latency': latency,
                        'type': 'tcp',
                        'timestamp': datetime.now().isoformat()
                    })
                    if success:
                        print("[%s] %s: TCP Connect Success, Latency %.2fms" % (
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S'), target, latency))
                    else:
                        print("[%s] %s: TCP Connect Failed" % (
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S'), target))

                # Calculate remaining time and wait
                elapsed = float(time.time() - start_time)
                remaining = max(0.0, float(PING_INTERVAL) - elapsed)
                time.sleep(remaining)
            except Exception as e:
                # 捕获所有异常，防止线程崩溃
                print("[%s] Error in tcp_worker for %s: %s" % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), target, str(e)))
                # 等待一段时间后继续
                time.sleep(PING_INTERVAL)

    def start_test(self, duration, extra_targets=None, no_default_nodes=False):
        """
        Start network testing
        增强参数说明和用户友好性
        """
        start_time = float(time.time())
        end_time = float(start_time + float(duration))
        threads = []

        print("=" * 80)
        print("网络连接稳定性测试")
        print("=" * 80)
        print("测试配置:")
        print("  测试时长: %d 秒 (%.1f 小时)" % (duration, duration / 3600.0))
        print("  预计结束时间: %s" %
              datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S'))
        
        if not no_default_nodes:
            print("  默认节点数: %d" % len(NODE_IPS))
            if len(NODE_IPS) <= 10:
                print("  默认节点列表: %s" % ", ".join(NODE_IPS))
            else:
                print("  默认节点列表: %s ... (共%d个)" % (", ".join(NODE_IPS[:10]), len(NODE_IPS)))
        
        if extra_targets:
            print("  额外目标数: %d" % len(extra_targets))
            print("  额外目标: %s" % ", ".join(extra_targets))
        else:
            print("  额外目标: 无")
        
        print("  测试间隔: %d 秒" % PING_INTERVAL)
        print("  Ping超时: %d 秒" % PING_TIMEOUT)
        print("  TCP超时: %d 秒" % TCP_TIMEOUT)
        print("=" * 80)
        print("开始测试... (按 Ctrl+C 可中断)")
        print("=" * 80)

        # Start ICMP test threads for default nodes
        if not no_default_nodes:
            for ip in NODE_IPS:
                try:
                    # 验证IP地址
                    ip = self._validate_host(ip)
                    t = threading.Thread(target=self.ping_worker, args=(ip,))
                    t.daemon = True
                    t.start()
                    threads.append(t)
                    self.active_threads.append(t)
                except ValueError as e:
                    print("[WARNING] 跳过无效的默认节点 %s: %s" % (ip, str(e)))

        # Start test threads for extra targets
        if extra_targets:
            for target in extra_targets:
                try:
                    if ':' in target:
                        # 验证TCP目标格式
                        host, port_str = target.split(':', 1)
                        self._validate_host(host)
                        port = int(port_str)
                        if not (1 <= port <= 65535):
                            raise ValueError("Port out of range: %d" % port)
                        t = threading.Thread(target=self.tcp_worker, args=(target,))
                    else:
                        # 验证主机
                        target = self._validate_host(target)
                        t = threading.Thread(target=self.ping_worker, args=(target,))
                    t.daemon = True
                    t.start()
                    threads.append(t)
                    self.active_threads.append(t)
                except (ValueError, TypeError) as e:
                    print("[WARNING] 跳过无效的目标 %s: %s" % (target, str(e)))

        # Wait for test duration
        try:
            while float(time.time()) < end_time:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n[INFO] 测试被用户中断")
        finally:
            # Stop all worker threads - 确保资源正确清理
            self.stop_event.set()
            # 给线程一些时间完成当前操作
            time.sleep(0.5)
            for t in threads:
                try:
                    t.join(timeout=2)  # 增加超时时间，确保线程正确退出
                except Exception as e:
                    print("Warning: Error joining thread: %s" % str(e))
            
            # 清理活动线程列表
            self.active_threads = []

        return self.results


def generate_report(results):
    """
    生成综合测试报告
    参照dbclone.py和dbmigration.py的风格，提供清晰的报告格式
    """
    if not results:
        return "=" * 80 + "\n测试报告\n" + "=" * 80 + "\n\n无测试数据\n"
    
    report = []
    report.append("=" * 80)
    report.append("网络连接稳定性测试报告")
    report.append("生成时间: %s" % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    report.append("=" * 80)
    report.append("")

    for target in sorted(results.keys()):
        data = results[target]
        if not data:
            continue

        test_type = data[0]['type']

        if test_type == 'icmp':
            total_tests = len(data)
            timeout_count = sum(1 for d in data if d['loss'] == 100)
            timeout_percent = (float(timeout_count) / float(total_tests)) * 100.0

            successful_pings = [d for d in data if d['loss'] < 100 and d['avg'] is not None]
            if successful_pings:
                avg_latency = float(sum(float(d['avg']) for d in successful_pings)) / float(len(successful_pings))
                min_latency = float(min(float(d['min']) for d in successful_pings))
                max_latency = float(max(float(d['max']) for d in successful_pings))
                jitter = float(max_latency - min_latency)
            else:
                avg_latency = min_latency = max_latency = jitter = 0.0

            report.append("节点: %s (ICMP Ping测试)" % target)
            report.append("  总测试次数: %d" % total_tests)
            report.append("  超时次数: %d (%.1f%%)" % (timeout_count, timeout_percent))
            if successful_pings:
                report.append("  平均延迟: %.2f ms" % avg_latency)
                report.append("  最小延迟: %.2f ms" % min_latency)
                report.append("  最大延迟: %.2f ms" % max_latency)
                report.append("  抖动: %.2f ms" % jitter)
            else:
                report.append("  无成功连接")

            # 状态评估
            status_ok = True
            if timeout_percent > 5:
                report.append("  ⚠️  [警告] 丢包率过高 (>5%%)")
                status_ok = False
            elif successful_pings and avg_latency > 100:
                report.append("  ⚠️  [警告] 平均延迟过高 (>100ms)")
                status_ok = False
            elif successful_pings and jitter > 50:
                report.append("  ⚠️  [警告] 抖动过大 (>50ms)")
                status_ok = False
            
            if status_ok and successful_pings:
                report.append("  ✅ 连接质量良好")

        elif test_type == 'tcp':
            total_tests = len(data)
            success_count = sum(1 for d in data if d['success'])
            success_percent = (float(success_count) / float(total_tests)) * 100.0

            successful_conns = [d for d in data if d['success']]
            if successful_conns:
                avg_latency = float(sum(float(d['latency']) for d in successful_conns)) / float(len(successful_conns))
                min_latency = float(min(float(d['latency']) for d in successful_conns))
                max_latency = float(max(float(d['latency']) for d in successful_conns))
                jitter = float(max_latency - min_latency)
            else:
                avg_latency = min_latency = max_latency = jitter = 0

            report.append("目标: %s (TCP连接测试)" % target)
            report.append("  总测试次数: %d" % total_tests)
            report.append("  成功率: %.1f%%" % success_percent)
            if successful_conns:
                report.append("  平均延迟: %.2f ms" % avg_latency)
                report.append("  最小延迟: %.2f ms" % min_latency)
                report.append("  最大延迟: %.2f ms" % max_latency)
                report.append("  抖动: %.2f ms" % jitter)
            else:
                report.append("  无成功连接")

            # 状态评估
            status_ok = True
            if success_percent < 95:
                report.append("  ⚠️  [警告] 连接成功率过低 (<95%%)")
                status_ok = False
            elif successful_conns and avg_latency > 200:
                report.append("  ⚠️  [警告] 连接延迟过高 (>200ms)")
                status_ok = False
            
            if status_ok and successful_conns:
                report.append("  ✅ 连接质量良好")

        report.append("-" * 60)
        report.append("")

    # 添加总结
    total_targets = len(results)
    report.append("=" * 80)
    report.append("测试总结")
    report.append("=" * 80)
    report.append("测试目标总数: %d" % total_targets)
    report.append("=" * 80)

    return "\n".join(report)


def validate_target(target):
    """
    验证目标格式 - 支持IP、IP:PORT、域名、域名:PORT
    基于Python官方最佳实践：严格验证所有用户输入
    """
    if not target:
        raise ValueError("目标不能为空")
    
    # 确保target是字符串（Python 3.6+）
    if isinstance(target, bytes):
        target = target.decode('utf-8')
    if not isinstance(target, str):
        raise ValueError("目标必须是字符串类型")
    
    if ':' in target:
        parts = target.split(':', 1)  # Split only on first colon
        if len(parts) != 2:
            raise ValueError("无效的目标格式 - 应为 HOST 或 HOST:PORT")

        host_part, port_part = parts

        # Validate host part (IP or domain)
        try:
            # First try to validate as IP address
            # 确保host_part是字符串
            if isinstance(host_part, bytes):
                host_part = host_part.decode('utf-8')
            ipaddress.ip_address(host_part)
        except (ValueError, AttributeError):
            # If not an IP, validate as domain name
            if not re.match(
                    r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$',
                    host_part):
                raise ValueError("无效的主机 - 必须是有效的IP地址或域名")

        # Validate port part
        try:
            port = int(port_part)
            if not 1 <= port <= 65535:
                raise ValueError("端口号超出范围 (1-65535)")
        except ValueError:
            raise ValueError("无效的端口号")

        return target
    else:
        # Validate as plain host (IP or domain)
        try:
            # First try to validate as IP address
            # 确保target是字符串（已在函数开头处理过，但为了安全再次检查）
            if isinstance(target, bytes):
                target = target.decode('utf-8')
            ipaddress.ip_address(target)
            return target
        except (ValueError, AttributeError):
            # If not an IP, validate as domain name
            if not re.match(
                    r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$',
                    target):
                raise ValueError("无效的目标 - 必须是有效的IP地址或域名")
            return target


def parse_arguments():
    """
    解析命令行参数 - 参照dbclone.py和dbmigration.py的风格
    提供完整的帮助文档和使用示例
    """
    parser = argparse.ArgumentParser(
        description='网络连接稳定性测试工具 - 支持ICMP和TCP连接测试',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:

  # 使用默认配置测试默认节点（2小时）
  %(prog)s

  # 测试指定时长（1小时 = 3600秒）
  %(prog)s -d 3600

  # 测试默认节点 + 额外IP地址
  %(prog)s 192.168.1.100 192.168.1.101

  # 测试默认节点 + TCP端口连接
  %(prog)s 192.168.1.100:3306 192.168.1.100:8080

  # 测试默认节点 + 域名
  %(prog)s example.com

  # 测试默认节点 + 域名和端口
  %(prog)s api.example.com:443

  # 完整示例：1小时测试，包含多个目标
  %(prog)s -d 3600 192.168.1.100 192.168.1.100:3306 api.example.com:443

  # 仅测试额外目标（不测试默认节点）
  %(prog)s --no-default-nodes 192.168.1.100 192.168.1.101

目标格式说明:
  - IP地址: 192.168.1.100 (ICMP测试)
  - IP:端口: 192.168.1.100:3306 (TCP连接测试)
  - 域名: example.com (ICMP测试)
  - 域名:端口: api.example.com:443 (TCP连接测试)

默认配置:
  - 测试时长: 7200秒 (2小时)
  - 默认节点: 预定义的NODE_IPS列表
  - Ping间隔: 1秒
  - Ping超时: 2秒
  - TCP超时: 2秒

报告输出:
  - 实时输出: 控制台实时显示测试结果
  - 测试报告: 自动保存为 network_test_report_YYYYMMDD_HHMMSS.txt
        """
    )

    parser.add_argument('-d', '--duration', type=int, default=DEFAULT_TEST_DURATION,
                        help='测试时长（秒），默认: %d (2小时)' % DEFAULT_TEST_DURATION)
    parser.add_argument('--no-default-nodes', action='store_true',
                        help='不测试默认节点，仅测试指定的额外目标')
    parser.add_argument('targets', nargs='*',
                        help='额外的测试目标（IP地址、IP:端口、域名或域名:端口）')

    args = parser.parse_args()

    # 验证duration
    if args.duration <= 0:
        parser.error("测试时长必须大于0")

    # 验证所有目标
    validated_targets = []
    for target in args.targets:
        try:
            validated_targets.append(validate_target(target))
        except ValueError as e:
            parser.error("无效的目标 '%s': %s" % (target, str(e)))

    return args.duration, validated_targets, args.no_default_nodes


def main():
    """主函数 - 参照dbclone.py和dbmigration.py的风格"""
    try:
        duration, extra_targets, no_default_nodes = parse_arguments()

        # 检查是否有任何目标
        if no_default_nodes and not extra_targets:
            print("错误: 使用 --no-default-nodes 时必须指定至少一个测试目标")
            print("使用 %s --help 查看帮助信息" % sys.argv[0])
            sys.exit(1)

        # Start testing
        tester = NetworkTester()
        results = tester.start_test(duration, extra_targets, no_default_nodes)

        # Generate report
        report = generate_report(results)
        print("\n" + report)

        # Save report to file
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = 'network_test_report_%s.txt' % timestamp
        try:
            with open(filename, 'w') as f:
                f.write(report)
            print("\n" + "=" * 80)
            print("✅ 测试报告已保存: %s" % filename)
            print("=" * 80)
        except IOError as e:
            print("\n[WARNING] 无法保存测试报告到文件: %s" % str(e))
            print("报告内容已显示在上方")

    except KeyboardInterrupt:
        print("\n\n[INFO] 测试被用户中断")
        sys.exit(0)
    except argparse.ArgumentError as e:
        print("参数错误: %s" % str(e))
        sys.exit(1)
    except Exception as e:
        print("错误: %s" % str(e))
        if '--verbose' in sys.argv or '-v' in sys.argv:
            print("详细错误信息:")
            print(traceback.format_exc())
        sys.exit(1)


if __name__ == '__main__':
    main()

