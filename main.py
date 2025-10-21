#!/usr/bin/env python3
"""
main.py
Interactive Kubernetes cluster health monitoring shell with enhanced dashboard

"""
import os
import sys
import cmd
import time
import signal
import select
import readchar
import platform
import subprocess
from rich.live import Live
from rich.text import Text
from collections import defaultdict
from kubernetes import client, config
from shlex import split as shlex_split
from rich.console import Console, Group
from kubernetes.client import CustomObjectsApi
from kubernetes.client.rest import ApiException
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from kubernetes.config.kube_config import list_kube_config_contexts, load_kube_config
try:
    import msvcrt  # Windows non-blocking keyboard
except Exception:
    msvcrt = None
try:
    import tty, termios, fcntl  # POSIX raw mode
except Exception:
    tty = None
    termios = None
import threading

console = Console()

# # Flag toggled during live mode to normalize styles for Windows
_LIVE_MODE_ACTIVE = False
_USE_RICH_MARKUP = False

# ===================== Colors =====================
CLR = {
    'reset': '\033[0m',
    'red': '\033[31m',
    'yellow': '\033[33m',
    'green': '\033[32m',
    'cyan': '\033[36m',
    'orange': '\033[38;5;214m',
    'purple': '\033[35m',
    'bold': '\033[1m',
    'highlight': '\033[7m',  # inverted colors
}

# re-use our console instance (assumed imported earlier)
INTERACTIVE_SUBCOMMANDS = {
    "auth",
    "generate",
    "serve",
    "integration",
    "completion", 
    "custom-analyzer",
}


def colorize(text, color=None, bold=False, highlight=False):
    """
    Colorize text consistently for live and non-live modes.
    - Uses Rich markup in live mode (_USE_RICH_MARKUP=True)
    - Uses ANSI codes in non-live mode
    """
    # Define unified color mapping
    COLOR_MAP = {
        'orange': '#ffaf00',
        'red': 'red',
        'green': 'green',
        'yellow': 'yellow',
        'cyan': 'cyan',
        'purple': 'purple',
    }

    # --- Live mode / Rich markup ---
    if _USE_RICH_MARKUP:
        style_tokens = []
        if bold:
            style_tokens.append('bold')
        if highlight:
            style_tokens.append('reverse')
        if color:
            style_tokens.append(COLOR_MAP.get(color, color))
        style_str = ' '.join(style_tokens) if style_tokens else ''
        if style_str:
            return f"[{style_str}]{text}[/]"
        return text

    # --- Non-live / ANSI ---
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        return text

    parts = []

    # Unified bold handling
    if bold:
        parts.append(CLR.get('bold', ''))

    # Unified color handling
    if color and color in COLOR_MAP:
        # Convert Rich hex to closest ANSI if possible
        if color == 'orange':
            parts.append('\033[38;5;214m')  # ANSI approximation of #ffaf00
        else:
            parts.append(CLR.get(color, ''))

    if highlight:
        parts.append(CLR.get('highlight', ''))

    parts.append(text)
    parts.append(CLR.get('reset', '\033[0m'))

    return ''.join(parts)

def center_text(text, width=60):
    return text.center(width)

# ===================== Smart Pod Hints =====================
POD_HINTS = {
    'ImagePullBackOff': 'Check image name or registry credentials',
    'ErrImagePull': 'Check image name or registry credentials',
    'CrashLoopBackOff': 'Pod is crashing repeatedly — check logs via kubectl logs <pod>',
    'ContainerCreating': 'Pod is being scheduled — wait or check node resources',
    'CreateContainerConfigError': 'Check container configuration or secrets',
    'OOMKilled': 'Pod was killed due to memory limits — consider increasing resources'
}

# ===================== Data Fetching =====================
def get_all_namespaces(v1):
    ns_raw = v1.list_namespace()
    return [ns.metadata.name for ns in ns_raw.items]

def get_nodes(v1):
    nodes = []
    for n in v1.list_node().items:
        node_info = {'name': n.metadata.name, 'issues': []}
        for cond in n.status.conditions or []:
            if cond.type == 'Ready' and cond.status != 'True':
                node_info['issues'].append(('Ready', 'NotReady', 'error'))
            elif cond.type in ['MemoryPressure', 'DiskPressure', 'PIDPressure'] and cond.status == 'True':
                node_info['issues'].append((cond.type, cond.status, 'warning'))
        if any(sev=='error' for _,_,sev in node_info['issues']):
            node_info['overall'] = 'error'
        elif node_info['issues']:
            node_info['overall'] = 'warning'
        else:
            node_info['overall'] = 'healthy'
        nodes.append(node_info)
    return nodes

def get_pods(v1, namespace):
    pods_by_ns = defaultdict(list)
    pods_raw = v1.list_namespaced_pod(namespace)
    for pod in pods_raw.items:
        containers = []
        for cs in pod.status.container_statuses or []:
            state = cs.state
            s = pod.status.phase
            severity = None
            hint = None
            if state.waiting:
                s = state.waiting.reason or 'Waiting'
                if s in POD_HINTS:
                    severity = 'error'
                    hint = POD_HINTS[s]
                else:
                    severity = 'warning'
                    hint = f"Pod waiting: {s}"
            elif state.terminated:
                s = state.terminated.reason or 'Terminated'
                severity = 'error'
                hint = "Container terminated — check logs via 'kubectl logs <pod>'"
            containers.append({
                'name': cs.name,
                'ready': cs.ready,
                'restartCount': cs.restart_count,
                'state': s,
                'severity': severity,
                'hint': hint
            })
        pods_by_ns[namespace].append({
            'pod': pod.metadata.name,
            'phase': pod.status.phase,
            'containers': containers
        })
    return pods_by_ns

def get_deployments(v1, namespace):
    deps_by_ns = defaultdict(list)
    apps = client.AppsV1Api()
    try:
        dep_list = apps.list_namespaced_deployment(namespace)
        for dep in dep_list.items:
            available = dep.status.available_replicas or 0
            desired = dep.status.replicas or 0
            if available < desired:
                deps_by_ns[namespace].append({'name': dep.metadata.name, 'available': available, 'desired': desired})
    except ApiException:
        pass
    return deps_by_ns

def get_statefulsets(v1, namespace):
    sts_by_ns = defaultdict(list)
    apps_v1 = client.AppsV1Api()
    try:
        for sts in apps_v1.list_namespaced_stateful_set(namespace).items:
            desired = sts.spec.replicas or 0
            ready = sts.status.ready_replicas or 0
            if ready < desired:
                sts_by_ns[namespace].append({'name': sts.metadata.name, 'desired': desired, 'ready': ready})
    except Exception:
        pass
    return sts_by_ns

def get_daemonsets(v1, namespace):
    ds_by_ns = defaultdict(list)
    apps = client.AppsV1Api()
    try:
        ds_list = apps.list_namespaced_daemon_set(namespace)
        for ds in ds_list.items:
            available = ds.status.number_available or 0
            desired = ds.status.desired_number_scheduled or 0
            if available < desired:
                ds_by_ns[namespace].append({'name': ds.metadata.name, 'available': available, 'desired': desired})
    except ApiException:
        pass
    return ds_by_ns

def get_events(v1, namespace):
    events_by_ns = defaultdict(list)
    try:
        events_raw = v1.list_namespaced_event(namespace)
        for e in events_raw.items:
            severity = None
            if e.type == 'Warning':
                severity = 'warning'
            elif e.type == 'Error' or 'Failed' in (e.reason or '') or 'Err' in (e.reason or ''):
                severity = 'error'
            if severity:
                events_by_ns[namespace].append({
                    'type': e.type,
                    'reason': e.reason,
                    'message': e.message,
                    'time': e.last_timestamp,
                    'severity': severity
                })
    except Exception:
        pass
    return events_by_ns

def get_istio_resources(v1, namespace):
    istio_by_ns = defaultdict(list)
    custom_api = client.CustomObjectsApi()
    istio_kinds = [
        ('VirtualService', 'networking.istio.io', 'v1beta1', 'virtualservices'),
        ('DestinationRule', 'networking.istio.io', 'v1beta1', 'destinationrules'),
        ('Gateway', 'networking.istio.io', 'v1beta1', 'gateways')
    ]
    for kind, group, version, plural in istio_kinds:
        try:
            items = custom_api.list_namespaced_custom_object(
                group=group, version=version, namespace=namespace, plural=plural
            )
            for i in items.get('items', []):
                istio_by_ns[namespace].append({'kind': kind, 'name': i['metadata']['name']})
        except ApiException:
            continue
    return istio_by_ns

def get_virtualservices(namespace):
    """Fetch Istio VirtualServices in a namespace"""
    istio_api = CustomObjectsApi()
    vs_list = []
    try:
        vs = istio_api.list_namespaced_custom_object(
            group="networking.istio.io",
            version="v1alpha3",
            namespace=namespace,
            plural="virtualservices"
        )
        for item in vs.get('items', []):
            vs_list.append(item['metadata']['name'])
    except Exception:
        pass
    return vs_list

def get_recent_events(v1, namespace, days=1):
    """Return events from the last `days` days that are warnings/errors."""
    events_by_ns = defaultdict(list)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        events_raw = v1.list_namespaced_event(namespace)
        for e in events_raw.items:
            if not e.last_timestamp:
                continue
            if e.last_timestamp < cutoff:
                continue
            severity = None
            if e.type == 'Warning':
                severity = 'warning'
            elif e.type == 'Error' or 'Failed' in (e.reason or '') or 'Err' in (e.reason or ''):
                severity = 'error'
            if severity:
                events_by_ns[namespace].append({
                    'type': e.type,
                    'reason': e.reason,
                    'message': e.message,
                    'time': e.last_timestamp,
                    'severity': severity
                })
    except Exception:
        pass
    return events_by_ns

def get_recent_pod_issues(v1, namespace, days=1):
    """
    Find pods that have restarted or crashed within the last `days` days.
    Uses containerStatuses and startTime.
    """
    pods_with_issues = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        pods_raw = v1.list_namespaced_pod(namespace)
        for pod in pods_raw.items:
            # Check pod age
            start_time = pod.status.start_time
            if start_time and start_time < cutoff:
                continue  # skip old pods

            for cs in pod.status.container_statuses or []:
                if cs.restart_count > 0:
                    pods_with_issues.append({
                        'pod': pod.metadata.name,
                        'restarts': cs.restart_count,
                        'container': cs.name,
                        'state': (
                            cs.state.waiting.reason if cs.state.waiting else
                            cs.state.terminated.reason if cs.state.terminated else "Running"
                        )
                    })
                elif cs.state.waiting and 'CrashLoopBackOff' in cs.state.waiting.reason:
                    pods_with_issues.append({
                        'pod': pod.metadata.name,
                        'restarts': cs.restart_count,
                        'container': cs.name,
                        'state': cs.state.waiting.reason
                    })
    except Exception:
        pass
    return pods_with_issues



# ===================== Formatters =====================
def format_pod(pod):
    lines = []
    for c in pod['containers']:
        if c['severity']:
            info = c['state']
            hint = f" — {c['hint']}" if c['hint'] else ''
            lines.append(f"{colorize('❌','red')} {colorize(pod['pod'],'orange')} ({colorize(info,'red' if c['severity']=='error' else 'yellow')}){hint}")
    return lines[0] if lines else None

def format_dep(dep):
    info = f"{dep['available']}/{dep['desired']} available"
    return f"{colorize('❌','red')} {colorize(dep['name'],'orange')} ({colorize(info,'red')})"

def format_statefulset(sts):
    info = f"{sts['ready']}/{sts['desired']} ready"
    return f"{colorize('❌','red')} {colorize(sts['name'],'orange')} ({colorize(info,'red')})"

def format_daemonset(ds):
    info = f"{ds['available']}/{ds['desired']} available"
    return f"{colorize('❌','red')} {colorize(ds['name'],'orange')} ({colorize(info,'red')})"

def format_event(e):
    col = 'red' if e['type'] == 'Error' or 'Failed' in e['reason'] else 'yellow'
    return colorize(f"{e['reason']} — {e['message']} ({e['time']})", col)

def format_istio(res):
    return f"{colorize('🟢','green')} {colorize(res['name'],'cyan')} ({res['kind']})"

# ===================== Interactive Selection =====================
def pick_namespace(v1):
    namespaces = get_all_namespaces(v1)
    if not namespaces:
        print(colorize("No namespaces found.", 'red'))
        return None
    index = 0
    print(colorize("Select a namespace (↑ ↓ to move, Enter to select):\n", 'cyan', bold=True))
    for ns in namespaces:
        print(colorize(ns, color='orange'))
    while True:
        key = readchar.readkey()
        if key == readchar.key.UP:
            index = (index - 1) % len(namespaces)
        elif key == readchar.key.DOWN:
            index = (index + 1) % len(namespaces)
        elif key == readchar.key.ENTER:
            return namespaces[index]
        print(f"\033[{len(namespaces)}F", end='')
        for i, ns in enumerate(namespaces):
            if i == index:
                print(colorize(ns.ljust(50), color='orange', bold=True, highlight=True))
            else:
                print(colorize(ns.ljust(50), color='orange'))

def pick_context():
    contexts, current_context = list_kube_config_contexts()
    if not contexts:
        print(colorize("No contexts found in kubeconfig.", 'red'))
        return None
    index = 0
    header = colorize("Select a Kubernetes context (↑ ↓ to move, Enter to select):", 'cyan', bold=True)
    num_lines = len(contexts) + 1
    print(header)
    for ctx in contexts:
        name = ctx['name']
        print(colorize(name, color='orange'))
    while True:
        key = readchar.readkey()
        if key == readchar.key.UP:
            index = (index - 1) % len(contexts)
        elif key == readchar.key.DOWN:
            index = (index + 1) % len(contexts)
        elif key == readchar.key.ENTER:
            return contexts[index]['name']
        print(f"\033[{num_lines}F", end='')
        print(header.ljust(80))
        for i, ctx in enumerate(contexts):
            name = ctx['name']
            if i == index:
                print(colorize(name.ljust(80), color='orange', bold=True, highlight=True))
            else:
                print(colorize(name.ljust(80), color='orange'))

def get_node_utilization(v1, v1_metrics):
    """
    Returns a dict of node_name -> {'cpu_percent': XX, 'mem_percent': YY}
    """
    node_metrics = {}
    try:
        metrics_res = v1_metrics.list_cluster_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            plural="nodes"
        )
        node_items = {n['metadata']['name']: n for n in metrics_res.get('items', [])}

        for node in v1.list_node().items:
            name = node.metadata.name

            # Node capacity
            cpu_capacity = int(node.status.capacity.get('cpu', 0)) * 1000  # cores -> millicores
            mem_capacity = int(node.status.capacity.get('memory', '0')[:-2]) / 1024  # Ki -> Mi

            # Node usage
            usage = node_items.get(name, {}).get('usage', {})
            cpu_usage_raw = usage.get('cpu', '0m')
            mem_usage_raw = usage.get('memory', '0Mi')

            # Convert CPU
            if cpu_usage_raw.endswith('n'):
                cpu_usage_m = int(cpu_usage_raw[:-1]) / 1e6
            elif cpu_usage_raw.endswith('m'):
                cpu_usage_m = int(cpu_usage_raw[:-1])
            else:
                cpu_usage_m = int(cpu_usage_raw) * 1000  # cores -> millicores

            # Convert memory
            if mem_usage_raw.endswith('Ki'):
                mem_usage_mi = int(mem_usage_raw[:-2]) / 1024
            elif mem_usage_raw.endswith('Mi'):
                mem_usage_mi = int(mem_usage_raw[:-2])
            elif mem_usage_raw.endswith('Gi'):
                mem_usage_mi = int(mem_usage_raw[:-2]) * 1024
            else:
                mem_usage_mi = int(mem_usage_raw) / (1024*1024)

            cpu_percent = round(cpu_usage_m / cpu_capacity * 100, 1) if cpu_capacity else 0
            mem_percent = round(mem_usage_mi / mem_capacity * 100, 1) if mem_capacity else 0

            node_metrics[name] = {'cpu_percent': cpu_percent, 'mem_percent': mem_percent}

    except Exception:
        pass

    return node_metrics


def get_pod_utilization(v1, v1_metrics, namespace):
    """
    Returns a dict of pod_name -> {'cpu_percent': XX, 'mem_percent': YY}
    Utilization is based on container limits if set, otherwise requests.
    """
    pod_metrics = {}
    try:
        metrics_res = v1_metrics.list_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods"
        )

        # Build a map pod_name -> container usage
        usage_map = {}
        for item in metrics_res.get('items', []):
            pod_name = item['metadata']['name']
            cpu_total = 0
            mem_total = 0
            for c in item['containers']:
                cpu_raw = c['usage']['cpu']
                mem_raw = c['usage']['memory']

                # CPU
                if cpu_raw.endswith('n'):
                    cpu_total += int(cpu_raw[:-1]) / 1e6
                elif cpu_raw.endswith('m'):
                    cpu_total += int(cpu_raw[:-1])
                else:
                    cpu_total += int(cpu_raw) * 1000

                # Memory
                if mem_raw.endswith('Ki'):
                    mem_total += int(mem_raw[:-2]) / 1024
                elif mem_raw.endswith('Mi'):
                    mem_total += int(mem_raw[:-2])
                elif mem_raw.endswith('Gi'):
                    mem_total += int(mem_raw[:-2]) * 1024

            usage_map[pod_name] = {'cpu_m': cpu_total, 'mem_mi': mem_total}

        # Get pod limits/requests from spec
        pods_spec = v1.list_namespaced_pod(namespace)
        for pod in pods_spec.items:
            pod_name = pod.metadata.name
            cpu_limit_total = 0
            mem_limit_total = 0
            for c in pod.spec.containers:
                # Limits first
                cpu_limit = 0
                mem_limit = 0
                if c.resources and c.resources.limits:
                    cpu_l = c.resources.limits.get('cpu')
                    mem_l = c.resources.limits.get('memory')
                    if cpu_l:
                        if str(cpu_l).endswith('m'):
                            cpu_limit += int(cpu_l[:-1])
                        else:
                            cpu_limit += int(cpu_l) * 1000
                    if mem_l:
                        if str(mem_l).endswith('Ki'):
                            mem_limit += int(mem_l[:-2]) / 1024
                        elif str(mem_l).endswith('Mi'):
                            mem_limit += int(mem_l[:-2])
                        elif str(mem_l).endswith('Gi'):
                            mem_limit += int(mem_l[:-2]) * 1024

                # Fallback to requests
                if (cpu_limit == 0 or mem_limit == 0) and c.resources and c.resources.requests:
                    cpu_r = c.resources.requests.get('cpu')
                    mem_r = c.resources.requests.get('memory')
                    if cpu_r and cpu_limit==0:
                        if str(cpu_r).endswith('m'):
                            cpu_limit += int(cpu_r[:-1])
                        else:
                            cpu_limit += int(cpu_r) * 1000
                    if mem_r and mem_limit==0:
                        if str(mem_r).endswith('Ki'):
                            mem_limit += int(mem_r[:-2]) / 1024
                        elif str(mem_r).endswith('Mi'):
                            mem_limit += int(mem_r[:-2])
                        elif str(mem_r).endswith('Gi'):
                            mem_limit += int(mem_r[:-2]) * 1024

                cpu_limit_total += cpu_limit
                mem_limit_total += mem_limit

            usage = usage_map.get(pod_name, {})
            cpu_percent = round(usage.get('cpu_m',0)/cpu_limit_total*100,1) if cpu_limit_total else 0
            mem_percent = round(usage.get('mem_mi',0)/mem_limit_total*100,1) if mem_limit_total else 0
            pod_metrics[pod_name] = {'cpu_percent': cpu_percent, 'mem_percent': mem_percent}

    except Exception:
        pass

    return pod_metrics

class _RawKeyboard:
    def __init__(self):
        self.fd = None
        self.old_termios = None
        self.old_flags = None

    def __enter__(self):
        if not (termios and tty and fcntl):
            return self
        if not sys.stdin.isatty():
            return self
        self.fd = sys.stdin.fileno()
        self.old_termios = termios.tcgetattr(self.fd)
        self.old_flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)

        # Disable echo + canonical mode; keep non-blocking reads for the loop
        new_attrs = termios.tcgetattr(self.fd)
        new_attrs[3] = new_attrs[3] & ~(termios.ECHO | termios.ICANON)
        termios.tcsetattr(self.fd, termios.TCSANOW, new_attrs)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, self.old_flags | os.O_NONBLOCK)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is None:
            return
        termios.tcsetattr(self.fd, termios.TCSANOW, self.old_termios)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, self.old_flags)

# ===================== Shell =====================
class KubeShell(cmd.Cmd):
    intro = "Welcome to k8s-monitor shell. Type help or ? to list commands.\n"
    prompt = colorize("k8s> ", 'green')
    width = 70

    def __init__(self, v1):
        super().__init__()
        self.v1 = v1



    def print_namespace(self, ns):
        """Fetch resources lazily and then print namespace"""
        # Fetch all resources for the namespace
        data = {}
        data['pods'] = get_pods(self.v1, ns)
        data['deployments'] = get_deployments(self.v1, ns)
        data['statefulsets'] = get_statefulsets(self.v1, ns)
        data['daemonsets'] = get_daemonsets(self.v1, ns)
        data['events'] = get_events(self.v1, ns)
        data['istio'] = get_istio_resources(self.v1, ns)
        data['virtualservices'] = get_virtualservices(ns)

        self.print_namespace_from_data(ns, data)
        # ===================== Cluster Command =====================


    def do_cluster(self, arg=None):
        """Show cluster nodes and namespaces with issues; CPU/MEM optional if metrics exist."""
        live_mode = arg and arg.strip() == "--live"
        # Force a stable color system to avoid live refresh downgrades on some terminals
        console = Console(force_terminal=True, color_system="truecolor", legacy_windows=False) if live_mode else None
        global _LIVE_MODE_ACTIVE
        _LIVE_MODE_ACTIVE = bool(live_mode)
        global _USE_RICH_MARKUP
        _USE_RICH_MARKUP = bool(live_mode)

        def display_cluster():
            lines = []

            # -------------------- Nodes --------------------
            nodes = get_nodes(self.v1)
            try:
                v1_metrics = client.CustomObjectsApi()
                node_metrics = get_node_utilization(self.v1, v1_metrics)
                metrics_available = True
            except Exception:
                node_metrics = {}
                metrics_available = False

            lines.append(colorize("=" * self.width, 'purple'))
            lines.append(colorize(center_text("CLUSTER NODES", self.width), 'purple', bold=True))
            lines.append(colorize("=" * self.width, 'purple'))

            for n in nodes:
                status_color = 'green' if n['overall'] == 'healthy' else ('red' if n['overall'] == 'error' else 'yellow')
                status_text = 'Ready' if n['overall'] == 'healthy' else ", ".join([f"{t}({val})" for t, val, _ in n['issues']])
                line = f"    {colorize(n['name'], 'orange').ljust(40)}"
                if metrics_available:
                    m = node_metrics.get(n['name'], {})
                    cpu = m.get('cpu_percent')
                    mem = m.get('mem_percent')
                    if cpu is not None and mem is not None:
                        line += f" CPU: {colorize(str(cpu)+'%', 'cyan')} MEM: {colorize(str(mem)+'%', 'cyan')} "
                line += colorize(status_text, status_color, bold=True)
                lines.append(line)

            lines.append("")

            # -------------------- Namespaces --------------------
            namespaces = get_all_namespaces(self.v1)
            if not namespaces:
                lines.append(colorize("No namespaces found.", 'red'))
                return lines

            lines.append(colorize(center_text("Checking namespaces...", self.width), 'cyan', bold=True))

            def fetch_namespace_data(ns):
                results = {}
                with ThreadPoolExecutor(max_workers=5) as exe:
                    future_map = {
                        exe.submit(get_pods, self.v1, ns): "pods",
                        exe.submit(get_deployments, self.v1, ns): "deployments",
                        exe.submit(get_statefulsets, self.v1, ns): "statefulsets",
                        exe.submit(get_daemonsets, self.v1, ns): "daemonsets",
                        exe.submit(get_events, self.v1, ns): "events",
                        exe.submit(get_istio_resources, self.v1, ns): "istio",
                        exe.submit(get_virtualservices, ns): "virtualservices"
                    }
                    for f in as_completed(future_map):
                        key = future_map[f]
                        try:
                            results[key] = f.result()
                        except Exception:
                            results[key] = None
                return ns, results

            namespace_data_list = []
            with ThreadPoolExecutor(max_workers=10) as exe:
                futures = [exe.submit(fetch_namespace_data, ns) for ns in namespaces]
                for future in as_completed(futures):
                    ns, data = future.result()
                    namespace_data_list.append((ns, data))

            for ns, data in sorted(namespace_data_list, key=lambda x: x[0]):
                pods_by_ns = data.get("pods", {})
                deps_by_ns = data.get("deployments", {})
                sts_by_ns = data.get("statefulsets", {})
                ds_by_ns = data.get("daemonsets", {})
                events_by_ns = data.get("events", {})
                istio_res = data.get("istio", {}).get(ns, [])
                vs_list = data.get("virtualservices", [])

                # Only show namespaces that actually have issues/resources of interest
                if not any([pods_by_ns.get(ns), deps_by_ns.get(ns), sts_by_ns.get(ns), ds_by_ns.get(ns),
                            events_by_ns.get(ns), istio_res, vs_list]):
                    continue

                pod_metrics = {}
                try:
                    if pods_by_ns or deps_by_ns:
                        pod_metrics = get_pod_utilization(self.v1, client.CustomObjectsApi(), ns)
                except Exception:
                    pod_metrics = {}

                lines.append(colorize("=" * self.width, 'purple'))
                lines.append(colorize(center_text(f"NAMESPACE: {ns}", self.width), 'purple', bold=True))
                lines.append(colorize("=" * self.width, 'purple'))

                # --- Pods ---
                pod_lines = []
                for p in pods_by_ns.get(ns, []):
                    line = format_pod(p)
                    if line:
                        m = pod_metrics.get(p['pod'], {}) if pod_metrics else {}
                        cpu = m.get('cpu_percent')
                        mem = m.get('mem_percent')
                        if cpu is not None and mem is not None:
                            pod_lines.append(f"{line} — CPU: {colorize(str(cpu)+'%', 'cyan')} MEM: {colorize(str(mem)+'%', 'cyan')}")
                        else:
                            pod_lines.append(line)
                if pod_lines:
                    lines.append(f"    {colorize('🟠 PODS', 'orange')}")
                    lines.append(colorize("─" * self.width, 'purple'))
                    lines.extend([f"        {pl}" for pl in pod_lines])
                    lines.append("")

                # --- Deployments ---
                if deps_by_ns.get(ns):
                    lines.append(f"    {colorize('🟠 DEPLOYMENTS', 'orange')}")
                    lines.append(colorize("─" * self.width, 'purple'))
                    lines.extend([f"        {format_dep(d)}" for d in deps_by_ns.get(ns, [])])
                    lines.append("")

                # --- StatefulSets ---
                if sts_by_ns.get(ns):
                    lines.append(f"    {colorize('🟠 STATEFULSETS', 'orange')}")
                    lines.append(colorize("─" * self.width, 'purple'))
                    lines.extend([f"        {format_statefulset(s)}" for s in sts_by_ns.get(ns, [])])
                    lines.append("")

                # --- DaemonSets ---
                if ds_by_ns.get(ns):
                    lines.append(f"    {colorize('🟠 DAEMONSETS', 'orange')}")
                    lines.append(colorize("─" * self.width, 'purple'))
                    lines.extend([f"        {format_daemonset(ds)}" for ds in ds_by_ns.get(ns, [])])
                    lines.append("")

                # --- Events ---
                if events_by_ns.get(ns):
                    lines.append(f"    {colorize('🟠 EVENTS', 'orange')}")
                    lines.append(colorize("─" * self.width, 'purple'))
                    lines.extend([f"        {format_event(e)}" for e in events_by_ns.get(ns, [])])
                    lines.append("")

                # --- Istio Resources ---
                if istio_res:
                    lines.append(f"    {colorize('🟠 ISTIO RESOURCES', 'orange')}")
                    lines.append(colorize("─" * self.width, 'purple'))
                    lines.extend([f"        {format_istio(r)}" for r in istio_res])
                    lines.append("")

                # --- VirtualServices ---
                if vs_list:
                    lines.append(f"    {colorize('🟠 ISTIO VIRTUALSERVICES', 'orange')}")
                    lines.append(colorize("─" * self.width, 'purple'))
                    lines.extend([f"        {colorize(vs, 'red')}" for vs in vs_list])
                    lines.append("")

            return lines

        if live_mode:
            def make_group(lines):
                parse = Text.from_markup if _USE_RICH_MARKUP else Text.from_ansi
                texts = []
                for line in lines:
                    t = parse(line)
                    # Ensure each logical line consumes exactly one row; prevent wrapping-caused height drift
                    t.no_wrap = True
                    t.overflow = "ellipsis"
                    texts.append(t)
                return Group(*texts)

            def get_key_nonblocking():
                # Windows
                if msvcrt:
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        if ch in ('q', 'Q'):
                            return 'QUIT'
                        if ch == '\x1b':
                            return 'ESC'
                        if ch == '\xe0':
                            code = msvcrt.getwch()
                            return {
                                'H': 'UP',   'P': 'DOWN',
                                'I': 'PGUP', 'Q': 'PGDN',
                                'G': 'HOME', 'O': 'END',
                            }.get(code)
                    return None

                # POSIX (Linux/macOS)
                if not (termios and tty and fcntl):
                    return None
                if not sys.stdin.isatty():
                    return None

                fd = sys.stdin.fileno()
                r, _, _ = select.select([fd], [], [], 0)
                if not r:
                    return None

                try:
                    buf = b''
                    while True:
                        try:
                            chunk = os.read(fd, 32)
                            if not chunk:
                                break
                            buf += chunk
                        except BlockingIOError:
                            break
                except Exception:
                    return None

                if not buf:
                    return None

                if buf in (b'q', b'Q'):
                    return 'QUIT'
                if buf == b'\x1b':
                    return 'ESC'

                if buf.startswith(b'\x1b'):
                    if buf.startswith(b'\x1b[A'):
                        return 'UP'
                    if buf.startswith(b'\x1b[B'):
                        return 'DOWN'
                    if buf.startswith(b'\x1b[5~') or buf.startswith(b'\x1b[5;'):
                        return 'PGUP'
                    if buf.startswith(b'\x1b[6~') or buf.startswith(b'\x1b[6;'):
                        return 'PGDN'
                    if buf.startswith(b'\x1bOH') or buf.startswith(b'\x1b[H') or buf.startswith(b'\x1b[1~'):
                        return 'HOME'
                    if buf.startswith(b'\x1bOF') or buf.startswith(b'\x1b[F') or buf.startswith(b'\x1b[4~'):
                        return 'END'
                    return 'ESC'

                ch = buf[:1].decode('utf-8', 'ignore')
                if ch in ('q', 'Q'):
                    return 'QUIT'
                return None

            # Live loop with scroll and quit, using background data refresh to reduce input lag
            scroll_offset = 0
            lines_snapshot = ["Initializing..."]
            snapshot_lock = threading.Lock()
            snapshot_version = {"v": 0}
            stop_event = threading.Event()

            def refresh_worker():
                while not stop_event.is_set():
                    try:
                        new_lines = display_cluster()
                        with snapshot_lock:
                            lines_snapshot[:] = new_lines
                            snapshot_version["v"] += 1
                    except Exception:
                        # Keep previous lines on error
                        pass
                    finally:
                        stop_event.wait(1.0)

            worker = threading.Thread(target=refresh_worker, daemon=True)
            worker.start()

            last_seen_version = -1
            last_offset = -1
            last_key = None
            last_key_time = 0.0
            repeat_count = 0

            try:
                with _RawKeyboard():
                    with Live(make_group(lines_snapshot), console=console, refresh_per_second=30, screen=True) as live:
                        while True:
                            with snapshot_lock:
                                all_lines = list(lines_snapshot)
                                current_version = snapshot_version["v"]

                            height = console.size.height
                            # Reserve exactly 1 line for footer to avoid dropping a data line
                            view_height = max(1, height - 1)
                            height = console.size.height
                            # Reserve exactly 1 line for footer to avoid dropping a data line
                            view_height = max(1, height - 1)
                            total = len(all_lines)
                            max_offset = max(0, total - view_height)
                            if scroll_offset > max_offset:
                                scroll_offset = max_offset
                            if scroll_offset < 0:
                                scroll_offset = 0

                            # Only update UI if data changed or scroll position changed
                            if current_version != last_seen_version or scroll_offset != last_offset:
                                visible = all_lines[scroll_offset:scroll_offset + view_height]
                                footer = colorize(f"[↑] Up  [↓] Down  [PgUp/PgDn] Page  [Q] Quit    {scroll_offset+1}-{min(scroll_offset+view_height,total)}/{total}", 'cyan')
                                render_lines = visible + [footer]
                                live.update(make_group(render_lines))
                                last_seen_version = current_version
                                last_offset = scroll_offset

                            # Handle input with high responsiveness and simple repeat acceleration
                            key = get_key_nonblocking()
                            if key in ('QUIT', 'ESC'):
                                break
                            elif key == 'DOWN':
                                scroll_offset += 1
                            elif key == 'UP':
                                scroll_offset -= 1
                            elif key == 'PGDN':
                                scroll_offset += view_height - 1
                            elif key == 'PGUP':
                                scroll_offset -= view_height - 1
                            elif key == 'HOME':
                                scroll_offset = 0
                            elif key == 'END':
                                scroll_offset = max_offset
                            time.sleep(0.02)
            finally:
                stop_event.set()
        else:
            print("\n".join(display_cluster()))



    def do_ns(self, arg):
        """Show single namespace resources."""
        global _LIVE_MODE_ACTIVE, _USE_RICH_MARKUP
        _LIVE_MODE_ACTIVE = False
        _USE_RICH_MARKUP = False
        ns = arg.strip() if arg.strip() else pick_namespace(self.v1)
        if ns:
            self.print_namespace(ns)

    def do_context(self, arg):
        """Switch Kubernetes context."""
        global _LIVE_MODE_ACTIVE, _USE_RICH_MARKUP
        _LIVE_MODE_ACTIVE = False
        _USE_RICH_MARKUP = False
        ctx_name = arg.strip() if arg.strip() else pick_context()
        if ctx_name:
            try:
                load_kube_config(context=ctx_name)
                self.v1 = client.CoreV1Api()
                print(colorize(f"Switched to context: {ctx_name}", 'green'))
            except Exception as e:
                print(colorize(f"Failed to switch context: {e}", 'red'))

    # ===================== Print Namespace (Lazy Loading) =====================
    def print_namespace_from_data(self, ns, data):
        """Print namespace resources using already fetched data (avoids double-fetching)."""
        width = self.width
        found_issues = False

        pods_by_ns = data.get("pods", {})
        deps_by_ns = data.get("deployments", {})
        sts_by_ns = data.get("statefulsets", {})
        ds_by_ns = data.get("daemonsets", {})
        events_by_ns = data.get("events", {})
        istio_res = data.get("istio", {}).get(ns, [])
        vs_list = data.get("virtualservices", [])

        print(colorize("="*width,'purple'))
        print(colorize(center_text(f"NAMESPACE: {ns}", width), 'purple', bold=True))
        print(colorize("="*width,'purple'))

        # -------------------- Pods --------------------
        pod_lines = [format_pod(p) for p in pods_by_ns.get(ns, []) if format_pod(p)]
        if pod_lines:
            found_issues = True
            print(f"    {colorize('🟠 PODS', 'orange')}")
            print(colorize("─"*width,'purple'))
            for line in pod_lines:
                print(f"        {line}")
            print()

        # -------------------- Deployments --------------------
        ns_deps = deps_by_ns.get(ns, [])
        if ns_deps:
            found_issues = True
            print(f"    {colorize('🟠 DEPLOYMENTS', 'orange')}")
            print(colorize("─"*width,'purple'))
            for d in ns_deps:
                print(f"        {format_dep(d)}")
            print()

        # -------------------- StatefulSets --------------------
        ns_sts = sts_by_ns.get(ns, [])
        if ns_sts:
            found_issues = True
            print(f"    {colorize('🟠 STATEFULSETS', 'orange')}")
            print(colorize("─"*width,'purple'))
            for s in ns_sts:
                print(f"        {format_statefulset(s)}")
            print()

        # -------------------- DaemonSets --------------------
        ns_ds = ds_by_ns.get(ns, [])
        if ns_ds:
            found_issues = True
            print(f"    {colorize('🟠 DAEMONSETS', 'orange')}")
            print(colorize("─"*width,'purple'))
            for ds in ns_ds:
                print(f"        {format_daemonset(ds)}")
            print()

        # -------------------- Events --------------------
        ns_events = events_by_ns.get(ns, [])
        if ns_events:
            found_issues = True
            print(f"    {colorize('🟠 EVENTS', 'orange')}")
            print(colorize("─"*width,'purple'))
            for e in ns_events:
                print(f"        {format_event(e)}")
            print()

        # -------------------- Istio --------------------
        if istio_res:
            found_issues = True
            print(f"    {colorize('🟠 ISTIO RESOURCES', 'orange')}")
            print(colorize("─"*width,'purple'))
            for r in istio_res:
                print(f"        {format_istio(r)}")
            print()

        # -------------------- VirtualServices --------------------
        if vs_list:
            found_issues = True
            print(f"    {colorize('🟠 ISTIO VIRTUALSERVICES', 'orange')}")
            print(colorize("─"*width,'purple'))
            for vs in vs_list:
                print(f"        {colorize(vs, 'red')}")  # highlight as issue

        if not found_issues:
            print(colorize(f"✅ {ns} is healthy", 'green', bold=True))
            print()


    def do_diagnose(self, arg):
        """Show pod restarts, CPU/Memory utilization (%), pod state, and warning/error events.
        Usage:
        diagnose             -> one-shot
        diagnose --live      -> live mode with scrolling and refresh
        """
        args = (arg or "").split()
        live_mode = "--live" in args

        CPU_THRESHOLD = 75
        MEM_THRESHOLD = 75

        def build_lines():
            lines = []
            namespaces = get_all_namespaces(self.v1)
            any_issues = False
            try:
                v1_metrics = client.CustomObjectsApi()
            except Exception:
                v1_metrics = None
                lines.append(colorize("Warning: Metrics API not available", 'yellow'))

            for ns in namespaces:
                pods_by_ns = get_pods(self.v1, ns)
                events_by_ns = get_events(self.v1, ns)

                ns_has_issues = False
                pod_lines = []

                for pod in pods_by_ns.get(ns, []):
                    for cs in pod['containers']:
                        state_text = cs['state']
                        severity = cs.get('severity')
                        if severity == 'error':
                            state_col = 'red'
                        elif severity == 'warning':
                            state_col = 'yellow'
                        else:
                            state_col = 'green'

                        cpu_pct = mem_pct = None
                        cpu_col = mem_col = 'cyan'
                        if v1_metrics:
                            try:
                                metrics = v1_metrics.get_namespaced_custom_object(
                                    group="metrics.k8s.io",
                                    version="v1beta1",
                                    namespace=ns,
                                    plural="pods",
                                    name=pod['pod']
                                )
                                total_cpu_m = sum(int(c['usage']['cpu'].rstrip('n')) / 1e6 for c in metrics['containers'])
                                total_mem_bytes = sum(int(c['usage']['memory'].rstrip('Ki')) * 1024 for c in metrics['containers'])
                                pod_limits_cpu = sum(int(cc.get('cpu_limit', '100').rstrip('m')) for cc in pod['containers'])
                                pod_limits_mem = sum(int(cc.get('memory_limit', '128').rstrip('Mi'))*1024*1024 for cc in pod['containers'])
                                cpu_pct = round(total_cpu_m / pod_limits_cpu * 100, 1) if pod_limits_cpu else None
                                mem_pct = round(total_mem_bytes / pod_limits_mem * 100, 1) if pod_limits_mem else None
                                if cpu_pct and cpu_pct > CPU_THRESHOLD:
                                    cpu_col = 'red'
                                if mem_pct and mem_pct > MEM_THRESHOLD:
                                    mem_col = 'red'
                            except Exception:
                                pass

                        line = f"{colorize(pod['pod'], 'orange')} — State: {colorize(state_text, state_col)}"
                        if cs['restartCount'] > 0:
                            line += f" — Restarts: {colorize(str(cs['restartCount']), 'yellow')}"
                        if cpu_pct is not None:
                            line += f" — CPU: {colorize(str(cpu_pct)+'%', cpu_col)}"
                        if mem_pct is not None:
                            line += f" — MEM: {colorize(str(mem_pct)+'%', mem_col)}"
                        pod_lines.append(line)
                        if severity in ['error', 'warning'] or cs['restartCount'] > 0:
                            ns_has_issues = True

                event_lines = []
                for e in events_by_ns.get(ns, []):
                    if e['severity']:
                        event_lines.append(format_event(e))
                        ns_has_issues = True

                if ns_has_issues:
                    any_issues = True
                    lines.append(colorize("="*self.width,'purple'))
                    lines.append(colorize(center_text(f"NAMESPACE: {ns}", self.width), 'purple', bold=True))
                    lines.append(colorize("="*self.width,'purple'))
                    if pod_lines:
                        lines.append(f"    {colorize('🟠 PODS', 'orange')}")
                        lines.append(colorize("─"*self.width,'purple'))
                        lines.extend([f"        {l}" for l in pod_lines])
                        lines.append("")
                    if event_lines:
                        lines.append(f"    {colorize('🟠 EVENTS', 'orange')}")
                        lines.append(colorize("─"*self.width,'purple'))
                        lines.extend([f"        {l}" for l in event_lines])
                        lines.append("")

            if not any_issues:
                lines.append(colorize("✅ No pod restarts, high CPU/Memory, or warning/error events found.", 'green', bold=True))
            return lines

        if live_mode:
            console = Console(force_terminal=True, color_system="truecolor", legacy_windows=False)
            global _LIVE_MODE_ACTIVE, _USE_RICH_MARKUP
            _LIVE_MODE_ACTIVE = True
            _USE_RICH_MARKUP = True

            def make_group(lines):
                parse = Text.from_markup if _USE_RICH_MARKUP else Text.from_ansi
                texts = []
                for line in lines:
                    t = parse(line)
                    t.no_wrap = True
                    t.overflow = "ellipsis"
                    texts.append(t)
                return Group(*texts)

            def get_key_nonblocking():
                # Windows
                if msvcrt:
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        if ch in ('q', 'Q'):
                            return 'QUIT'
                        if ch == '\x1b':
                            return 'ESC'
                        if ch == '\xe0':
                            code = msvcrt.getwch()
                            return {
                                'H': 'UP',   'P': 'DOWN',
                                'I': 'PGUP', 'Q': 'PGDN',
                                'G': 'HOME', 'O': 'END',
                            }.get(code)
                    return None

                # POSIX (Linux/macOS)
                if not (termios and tty and fcntl):
                    return None
                if not sys.stdin.isatty():
                    return None

                fd = sys.stdin.fileno()
                r, _, _ = select.select([fd], [], [], 0)
                if not r:
                    return None

                try:
                    buf = b''
                    while True:
                        try:
                            chunk = os.read(fd, 32)
                            if not chunk:
                                break
                            buf += chunk
                        except BlockingIOError:
                            break
                except Exception:
                    return None

                if not buf:
                    return None

                if buf in (b'q', b'Q'):
                    return 'QUIT'
                if buf == b'\x1b':
                    return 'ESC'

                if buf.startswith(b'\x1b'):
                    if buf.startswith(b'\x1b[A'):
                        return 'UP'
                    if buf.startswith(b'\x1b[B'):
                        return 'DOWN'
                    if buf.startswith(b'\x1b[5~') or buf.startswith(b'\x1b[5;'):
                        return 'PGUP'
                    if buf.startswith(b'\x1b[6~') or buf.startswith(b'\x1b[6;'):
                        return 'PGDN'
                    if buf.startswith(b'\x1bOH') or buf.startswith(b'\x1b[H') or buf.startswith(b'\x1b[1~'):
                        return 'HOME'
                    if buf.startswith(b'\x1bOF') or buf.startswith(b'\x1b[F') or buf.startswith(b'\x1b[4~'):
                        return 'END'
                    return 'ESC'

                ch = buf[:1].decode('utf-8', 'ignore')
                if ch in ('q', 'Q'):
                    return 'QUIT'
                return None

            scroll_offset = 0
            lines_snapshot = ["Initializing..."]
            snapshot_lock = threading.Lock()
            snapshot_version = {"v": 0}
            stop_event = threading.Event()

            def refresh_worker():
                while not stop_event.is_set():
                    try:
                        new_lines = build_lines()
                        with snapshot_lock:
                            lines_snapshot[:] = new_lines
                            snapshot_version["v"] += 1
                    except Exception:
                        pass
                    finally:
                        stop_event.wait(1.0)

            threading.Thread(target=refresh_worker, daemon=True).start()

            last_seen_version = -1
            last_offset = -1
            try:
                with _RawKeyboard():
                    with Live(make_group(lines_snapshot), console=console, refresh_per_second=30, screen=True) as live:
                        while True:
                            with snapshot_lock:
                                all_lines = list(lines_snapshot)
                                current_version = snapshot_version["v"]
                            height = console.size.height
                            view_height = max(1, height - 1)
                            total = len(all_lines)
                            max_offset = max(0, total - view_height)
                            if scroll_offset > max_offset:
                                scroll_offset = max_offset
                            if scroll_offset < 0:
                                scroll_offset = 0
                            if current_version != last_seen_version or scroll_offset != last_offset:
                                visible = all_lines[scroll_offset:scroll_offset + view_height]
                                footer = colorize(f"[↑] Up  [↓] Down  [PgUp/PgDn] Page  [Q] Quit    {scroll_offset+1}-{min(scroll_offset+view_height,total)}/{total}", 'cyan')
                                live.update(make_group(visible + [footer]))
                                last_seen_version = current_version
                                last_offset = scroll_offset
                            key = get_key_nonblocking()
                            if key in ('QUIT', 'ESC'):
                                break
                            elif key == 'DOWN':
                                scroll_offset += 1
                            elif key == 'UP':
                                scroll_offset -= 1
                            elif key == 'PGDN':
                                scroll_offset += view_height - 1
                            elif key == 'PGUP':
                                scroll_offset -= view_height - 1
                            elif key == 'HOME':
                                scroll_offset = 0
                            elif key == 'END':
                                scroll_offset = max_offset
                            time.sleep(0.02)
            finally:
                stop_event.set()
            return

        # Non-live one-shot


        _LIVE_MODE_ACTIVE = False
        _USE_RICH_MARKUP = False
        print(colorize("🔎 Diagnosing cluster health...", 'cyan', bold=True))
        namespaces = get_all_namespaces(self.v1)
        any_issues = False
        try:
            v1_metrics = client.CustomObjectsApi()
        except Exception as e:
            v1_metrics = None
            print(colorize(f"Warning: Metrics API not available: {e}", 'yellow'))
        for ns in namespaces:
            pods_by_ns = get_pods(self.v1, ns)
            events_by_ns = get_events(self.v1, ns)
            ns_has_issues = False
            pod_lines = []
            for pod in pods_by_ns.get(ns, []):
                container_lines = []
                for cs in pod['containers']:
                    state_text = cs['state']
                    severity = cs.get('severity')
                    state_col = 'red' if severity == 'error' else ('yellow' if severity == 'warning' else 'green')
                    cpu_pct = mem_pct = None
                    cpu_col = mem_col = 'cyan'
                    if v1_metrics:
                        try:
                            metrics = v1_metrics.get_namespaced_custom_object(
                                group="metrics.k8s.io", version="v1beta1", namespace=ns, plural="pods", name=pod['pod']
                            )
                            total_cpu_m = sum(int(c['usage']['cpu'].rstrip('n')) / 1e6 for c in metrics['containers'])
                            total_mem_bytes = sum(int(c['usage']['memory'].rstrip('Ki')) * 1024 for c in metrics['containers'])
                            pod_limits_cpu = sum(int(cc.get('cpu_limit', '100').rstrip('m')) for cc in pod['containers'])
                            pod_limits_mem = sum(int(cc.get('memory_limit', '128').rstrip('Mi'))*1024*1024 for cc in pod['containers'])
                            cpu_pct = round(total_cpu_m / pod_limits_cpu * 100, 1) if pod_limits_cpu else None
                            mem_pct = round(total_mem_bytes / pod_limits_mem * 100, 1) if pod_limits_mem else None
                            if cpu_pct and cpu_pct > CPU_THRESHOLD: cpu_col = 'red'
                            if mem_pct and mem_pct > MEM_THRESHOLD: mem_col = 'red'
                        except Exception:
                            pass
                    line = f"{colorize(pod['pod'], 'orange')} — State: {colorize(state_text, state_col)}"
                    if cs['restartCount'] > 0:
                        line += f" — Restarts: {colorize(str(cs['restartCount']), 'yellow')}"
                    if cpu_pct is not None:
                        line += f" — CPU: {colorize(str(cpu_pct)+'%', cpu_col)}"
                    if mem_pct is not None:
                        line += f" — MEM: {colorize(str(mem_pct)+'%', mem_col)}"
                    container_lines.append(line)
                    if severity in ['error', 'warning'] or cs['restartCount'] > 0:
                        ns_has_issues = True
                pod_lines.extend(container_lines)
            event_lines = []
            for e in events_by_ns.get(ns, []):
                if e['severity']:
                    event_lines.append(format_event(e))
                    ns_has_issues = True
            if ns_has_issues:
                any_issues = True
                print(colorize("="*self.width,'purple'))
                print(colorize(center_text(f"NAMESPACE: {ns}", self.width), 'purple', bold=True))
                print(colorize("="*self.width,'purple'))
                if pod_lines:
                    print(f"    {colorize('🟠 PODS', 'orange')}")
                    print(colorize("─"*self.width,'purple'))
                    for l in pod_lines:
                        print(f"        {l}")
                    print()
                if event_lines:
                    print(f"    {colorize('🟠 EVENTS', 'orange')}")
                    print(colorize("─"*self.width,'purple'))
                    for l in event_lines:
                        print(f"        {l}")
                    print()
        if not any_issues:
            print(colorize("✅ No pod restarts, high CPU/Memory, or warning/error events found.", 'green', bold=True))

    

   

    def do_gpt(self, arg):
        """
        Run K8sGPT commands.
        Usage:
            gpt analyze --namespace default
            gpt version
            (alias: k8sgpt)
        """
        console = Console(force_terminal=True, color_system="truecolor", legacy_windows=True)
        if not arg:
            console.print("[yellow]Usage: gpt <command> [options][/yellow]")
            return

        cmd_parts = ["k8sgpt"] + shlex_split(arg)
        sub = cmd_parts[1] if len(cmd_parts) >= 2 else None
        interactive = sub in INTERACTIVE_SUBCOMMANDS

        try:
            # ✅ On Windows: use os.system to fully preserve color output
            if platform.system() == "Windows":
                full_cmd = " ".join(cmd_parts)
                exit_code = os.system(full_cmd)
                if exit_code != 0:
                    console.print(f"[red]k8sgpt exited with code {exit_code}[/red]")
                return

            # ✅ On Linux/macOS: use subprocess with direct stdio passthrough
            if interactive:
                completed = subprocess.run(
                    cmd_parts,
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr
                )
                if completed.returncode != 0:
                    console.print(f"[red]k8sgpt exited with code {completed.returncode}[/red]")
                return

            console.print(f"[blue]Running: {' '.join(cmd_parts)}[/blue]")
            with subprocess.Popen(
                cmd_parts,
                stdin=None,
                stdout=sys.stdout,   
                stderr=sys.stderr,
            ) as proc:
                try:
                    proc.wait()
                except KeyboardInterrupt:
                    proc.send_signal(signal.SIGINT)
                    proc.wait()
                    console.print("[yellow]Interrupted by user[/yellow]")
                finally:
                    if proc.poll() is None:
                        proc.terminate()

        except FileNotFoundError:
            console.print("[red]Error: k8sgpt is not installed or not in PATH[/red]")
        except Exception as e:
            console.print(f"[red]Unexpected error running k8sgpt: {e}[/red]")

    do_k8sgpt = do_gpt


    def do_resource(self, arg):
        """
        Show CPU and Memory utilization (%) for all pods.
        Usage:
        resource                 -> show for all namespaces
        resource <namespace>     -> show only for the given namespace
        resource [<ns>] --live    -> live mode with refresh and scrolling
        """
        args = (arg or "").split()
        live_mode = "--live" in args
        ns_filter = None
        for token in args:
            if token and not token.startswith('-'):
                ns_filter = token
                break

        if live_mode:
            console = Console(force_terminal=True, color_system="truecolor", legacy_windows=False)
            global _LIVE_MODE_ACTIVE, _USE_RICH_MARKUP
            _LIVE_MODE_ACTIVE = True
            _USE_RICH_MARKUP = True

            def build_lines():
                lines = []
                namespaces = get_all_namespaces(self.v1)
                if ns_filter:
                    if ns_filter not in namespaces:
                        return [colorize(f"Namespace '{ns_filter}' not found.", 'red')]
                    namespaces = [ns_filter]

                try:
                    v1_metrics = client.CustomObjectsApi()
                except Exception:
                    return [colorize("Metrics server not available. CPU/MEM metrics cannot be shown.", 'red')]

                CPU_THRESHOLD = 75
                MEM_THRESHOLD = 75

                any_ns = False
                for ns in sorted(namespaces):
                    pods_by_ns = get_pods(self.v1, ns)
                    pod_metrics_lines = []

                    for pod in sorted(pods_by_ns.get(ns, []), key=lambda x: x['pod']):
                        pod_name = pod['pod']
                        cpu_total_pct = mem_total_pct = None
                        cpu_col = mem_col = 'cyan'
                        try:
                            metrics = v1_metrics.get_namespaced_custom_object(
                                group="metrics.k8s.io",
                                version="v1beta1",
                                namespace=ns,
                                plural="pods",
                                name=pod_name
                            )
                            total_cpu_m = sum(int(c['usage']['cpu'].rstrip('n')) / 1e6 for c in metrics['containers'])
                            total_mem_bytes = sum(int(c['usage']['memory'].rstrip('Ki')) * 1024 for c in metrics['containers'])
                            pod_limits_cpu = sum(int(cc.get('cpu_limit', '100').rstrip('m')) for cc in pod['containers'])
                            pod_limits_mem = sum(int(cc.get('memory_limit', '128').rstrip('Mi')) * 1024 * 1024 for cc in pod['containers'])
                            cpu_total_pct = round(total_cpu_m / pod_limits_cpu * 100, 1) if pod_limits_cpu else None
                            mem_total_pct = round(total_mem_bytes / pod_limits_mem * 100, 1) if pod_limits_mem else None
                            if cpu_total_pct and cpu_total_pct > CPU_THRESHOLD:
                                cpu_col = 'red'
                            if mem_total_pct and mem_total_pct > MEM_THRESHOLD:
                                mem_col = 'red'
                        except Exception:
                            continue
                        line = f"{colorize(pod_name, 'orange')}"
                        if cpu_total_pct is not None:
                            line += f" — CPU: {colorize(str(cpu_total_pct)+'%', cpu_col)}"
                        if mem_total_pct is not None:
                            line += f" — MEM: {colorize(str(mem_total_pct)+'%', mem_col)}"
                        pod_metrics_lines.append(line)

                    if pod_metrics_lines:
                        any_ns = True
                        lines.append(colorize("="*self.width,'purple'))
                        lines.append(colorize(center_text(f"NAMESPACE: {ns}", self.width), 'purple', bold=True))
                        lines.append(colorize("="*self.width,'purple'))
                        lines.extend([f"    {l}" for l in pod_metrics_lines])
                        lines.append("")

                if not any_ns:
                    lines.append(colorize("No pod metrics available or metrics server not accessible.", 'yellow'))
                return lines

            def make_group(lines):
                parse = Text.from_markup if _USE_RICH_MARKUP else Text.from_ansi
                texts = []
                for line in lines:
                    t = parse(line)
                    t.no_wrap = True
                    t.overflow = "ellipsis"
                    texts.append(t)
                return Group(*texts)

            def get_key_nonblocking():
                # Windows
                if msvcrt:
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        if ch in ('q', 'Q'):
                            return 'QUIT'
                        if ch == '\x1b':
                            return 'ESC'
                        if ch == '\xe0':
                            code = msvcrt.getwch()
                            return {
                                'H': 'UP',   'P': 'DOWN',
                                'I': 'PGUP', 'Q': 'PGDN',
                                'G': 'HOME', 'O': 'END',
                            }.get(code)
                    return None

                # POSIX (Linux/macOS)
                if not (termios and tty and fcntl):
                    return None
                if not sys.stdin.isatty():
                    return None

                fd = sys.stdin.fileno()
                r, _, _ = select.select([fd], [], [], 0)
                if not r:
                    return None

                try:
                    buf = b''
                    while True:
                        try:
                            chunk = os.read(fd, 32)
                            if not chunk:
                                break
                            buf += chunk
                        except BlockingIOError:
                            break
                except Exception:
                    return None

                if not buf:
                    return None

                if buf in (b'q', b'Q'):
                    return 'QUIT'
                if buf == b'\x1b':
                    return 'ESC'

                if buf.startswith(b'\x1b'):
                    if buf.startswith(b'\x1b[A'):
                        return 'UP'
                    if buf.startswith(b'\x1b[B'):
                        return 'DOWN'
                    if buf.startswith(b'\x1b[5~') or buf.startswith(b'\x1b[5;'):
                        return 'PGUP'
                    if buf.startswith(b'\x1b[6~') or buf.startswith(b'\x1b[6;'):
                        return 'PGDN'
                    if buf.startswith(b'\x1bOH') or buf.startswith(b'\x1b[H') or buf.startswith(b'\x1b[1~'):
                        return 'HOME'
                    if buf.startswith(b'\x1bOF') or buf.startswith(b'\x1b[F') or buf.startswith(b'\x1b[4~'):
                        return 'END'
                    return 'ESC'

                ch = buf[:1].decode('utf-8', 'ignore')
                if ch in ('q', 'Q'):
                    return 'QUIT'
                return None

            # Background refresh and scrolling
            scroll_offset = 0
            lines_snapshot = ["Initializing..."]
            snapshot_lock = threading.Lock()
            snapshot_version = {"v": 0}
            stop_event = threading.Event()

            def refresh_worker():
                while not stop_event.is_set():
                    try:
                        new_lines = build_lines()
                        with snapshot_lock:
                            lines_snapshot[:] = new_lines
                            snapshot_version["v"] += 1
                    except Exception:
                        pass
                    finally:
                        stop_event.wait(1.0)

            worker = threading.Thread(target=refresh_worker, daemon=True)
            worker.start()

            last_seen_version = -1
            last_offset = -1
            try:
                with _RawKeyboard():
                    with Live(make_group(lines_snapshot), console=console, refresh_per_second=30, screen=True) as live:
                        while True:
                            with snapshot_lock:
                                all_lines = list(lines_snapshot)
                                current_version = snapshot_version["v"]
                            height = console.size.height
                            view_height = max(1, height - 1)
                            total = len(all_lines)
                            max_offset = max(0, total - view_height)
                            if scroll_offset > max_offset:
                                scroll_offset = max_offset
                            if scroll_offset < 0:
                                scroll_offset = 0
                            if current_version != last_seen_version or scroll_offset != last_offset:
                                visible = all_lines[scroll_offset:scroll_offset + view_height]
                                footer = colorize(f"[↑] Up  [↓] Down  [PgUp/PgDn] Page  [Q] Quit    {scroll_offset+1}-{min(scroll_offset+view_height,total)}/{total}", 'cyan')
                                live.update(make_group(visible + [footer]))
                                last_seen_version = current_version
                                last_offset = scroll_offset
                                
                            key = get_key_nonblocking()
                            if key in ('QUIT', 'ESC'):
                                break
                            elif key == 'DOWN':
                                scroll_offset += 1
                            elif key == 'UP':
                                scroll_offset -= 1
                            elif key == 'PGDN':
                                scroll_offset += view_height - 1
                            elif key == 'PGUP':
                                scroll_offset -= view_height - 1
                            elif key == 'HOME':
                                scroll_offset = 0
                            elif key == 'END':
                                scroll_offset = max_offset
                            time.sleep(0.02)
            finally:
                stop_event.set()
            return
        namespaces = get_all_namespaces(self.v1)
        if ns_filter:
            if ns_filter not in namespaces:
                print(colorize(f"Namespace '{ns_filter}' not found.", 'red'))
                return
            namespaces = [ns_filter]

        try:
            v1_metrics = client.CustomObjectsApi()
        except Exception:
            print(colorize("Metrics server not available. CPU/MEM metrics cannot be shown.", 'red'))
            return

        _LIVE_MODE_ACTIVE = False
        _USE_RICH_MARKUP = False  

        CPU_THRESHOLD = 75
        MEM_THRESHOLD = 75

        metrics_available = False  # Track if we get any metrics

        for ns in sorted(namespaces):
            pods_by_ns = get_pods(self.v1, ns)
            pod_metrics_lines = []

            for pod in sorted(pods_by_ns.get(ns, []), key=lambda x: x['pod']):
                pod_name = pod['pod']
                cpu_total_pct = mem_total_pct = None
                cpu_col = mem_col = 'cyan'

                try:
                    metrics = v1_metrics.get_namespaced_custom_object(
                        group="metrics.k8s.io",
                        version="v1beta1",
                        namespace=ns,
                        plural="pods",
                        name=pod_name
                    )
                    metrics_available = True  # At least one pod returned metrics

                    total_cpu_m = sum(int(c['usage']['cpu'].rstrip('n')) / 1e6 for c in metrics['containers'])
                    total_mem_bytes = sum(int(c['usage']['memory'].rstrip('Ki')) * 1024 for c in metrics['containers'])

                    pod_limits_cpu = sum(int(cc.get('cpu_limit', '100').rstrip('m')) for cc in pod['containers'])
                    pod_limits_mem = sum(int(cc.get('memory_limit', '128').rstrip('Mi')) * 1024 * 1024 for cc in pod['containers'])

                    cpu_total_pct = round(total_cpu_m / pod_limits_cpu * 100, 1) if pod_limits_cpu else None
                    mem_total_pct = round(total_mem_bytes / pod_limits_mem * 100, 1) if pod_limits_mem else None

                    if cpu_total_pct and cpu_total_pct > CPU_THRESHOLD:
                        cpu_col = 'red'
                    if mem_total_pct and mem_total_pct > MEM_THRESHOLD:
                        mem_col = 'red'

                except Exception:
                    continue  # silently skip pods without metrics

                line = f"{colorize(pod_name, 'orange')}"
                if cpu_total_pct is not None:
                    line += f" — CPU: {colorize(str(cpu_total_pct)+'%', cpu_col)}"
                if mem_total_pct is not None:
                    line += f" — MEM: {colorize(str(mem_total_pct)+'%', mem_col)}"
                pod_metrics_lines.append(line)

            if pod_metrics_lines:
                print(colorize("="*self.width,'purple'))
                print(colorize(center_text(f"NAMESPACE: {ns}", self.width), 'purple', bold=True))
                print(colorize("="*self.width,'purple'))
                for l in pod_metrics_lines:
                    print(f"    {l}")
                print()

        # If after processing all pods we got no metrics at all
        if not metrics_available:
            print(colorize("Metrics server not available. CPU/MEM metrics cannot be shown.", 'red'))

    def do_exit(self, arg): return True
    def do_quit(self, arg): return True
    def do_help(self, arg=None):
        print(colorize("Available commands:", 'cyan'))
        print(f"  {colorize('cluster [--live]', 'yellow')} — Show cluster errors/warnings (all namespaces, nodes). Use '--live' for real-time updates.")
        print(f"  {colorize('ns', 'yellow')} — Show a single namespace errors/warnings.")
        print(f"  {colorize('diagnose [--live]', 'yellow')} — Show recent errors, warnings, pods CPU and memory utilization, and pod restarts. Use '--live' for real-time monitoring.")
        print(f"  {colorize('context', 'yellow')} — Switch Kubernetes context.")
        print(f"  {colorize('resource [<namespace>] [--live]', 'yellow')} — Show pods CPU and Memory utilization (%). Optionally specify a namespace and/or '--live' for continuous updates.")
        print(f"  {colorize('k8sgpt | gpt', 'yellow')} — Interact with K8sGPT for analysis, recommendations, or troubleshooting using AI.")
        print(f"  {colorize('exit/quit', 'yellow')} — Exit shell.")
    

# ===================== Main =====================
def main():
    print(colorize("Loading kubeconfig...", 'cyan'))
    try:
        config.load_kube_config()
    except Exception as e:
        print(colorize(f"Error loading kubeconfig: {e}", 'red'))
        sys.exit(1)

    v1 = client.CoreV1Api()
    print(colorize(f"Connected to cluster: {client.Configuration().host}", 'green'))
    KubeShell(v1).cmdloop()

if __name__ == "__main__":
    main()


