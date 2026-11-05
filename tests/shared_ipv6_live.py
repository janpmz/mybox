"""Test the installed shared fork. Run as the signed-in owner after sudo -v.

Use MYBOX_VPN_HOST and MYBOX_VPN_PORT to include a known real VPN-only TCP
listener. A dedicated temporary utun exercises VPN routing without credentials.
The capture filter selects only packets to the local router on this run's port.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from network_isolation import PROBE
from reboot_network import host_ipv6, lan_configuration, TCP_PROBE

ROOT = Path(__file__).resolve().parents[1]
BOX = str(ROOT / 'mybox')
CLI = '/usr/local/bin/container'
PRIVATE = ['/usr/bin/sudo', '-n', '/bin/launchctl', 'asuser', str(pwd.getpwnam('_mybox').pw_uid),
           '/usr/bin/sudo', '-n', '-H', '-u', '_mybox', '--', CLI]
PREFIX = 'io.github.janpmz.my-container-box'
REPORT = ROOT / 'test-results/shared-ipv6-firewall-latest.json'
UDP_DNS = r'''
use Socket qw(AF_INET AF_INET6 SOCK_DGRAM IPPROTO_UDP inet_pton pack_sockaddr_in pack_sockaddr_in6);
my ($ip) = @ARGV; my $v6 = index($ip,':') >= 0; my $family = $v6 ? AF_INET6 : AF_INET;
my $addr = inet_pton($family,$ip) or die "bad address";
my $peer = $v6 ? pack_sockaddr_in6(53,$addr) : pack_sockaddr_in(53,$addr);
socket(my $s,$family,SOCK_DGRAM,IPPROTO_UDP) or die "socket: $!"; connect($s,$peer) or die "connect: $!";
local $SIG{ALRM} = sub { die "timeout\n" }; alarm 5;
my $query = pack('n6',0x5931,256,1,0,0,0)."\x07example\x03com\0".pack('n2',1,1);
send($s,$query,0) or die "send: $!"; defined recv($s,my $reply,2048,0) or die "recv: $!";
my ($id,$flags,$qd,$an)=unpack('n4',$reply);
die "bad DNS response" unless $id==0x5931 && ($flags&0x8000) && !($flags&15) && $an;
print "DNS reply\n";
'''
BOOT = r'''
use strict; use warnings; use IO::Handle;
use Socket qw(AF_INET AF_INET6 SOCK_DGRAM inet_pton pack_sockaddr_in pack_sockaddr_in6);
my ($router4,$router6,$port,$token)=@ARGV;
mkdir '/workspace'; open my $log,'>','/workspace/probes' or die $!; $log->autoflush(1);
sub probe {
    my ($family,$ip,$label)=@_;
    socket(my $s,$family,SOCK_DGRAM,0) or die $!;
    my $address=inet_pton($family,$ip) or die $!;
    my $dst=$family == AF_INET ? pack_sockaddr_in($port,$address) : pack_sockaddr_in6($port,$address);
    my $sent=send($s,$token.$label,0,$dst);
    print $log "$label ".(defined $sent?'sent':"error: $!")."\n";
    return defined $sent;
}
probe(AF_INET,$router4,'boot4');
for (1..80) { last if probe(AF_INET6,$router6,'boot6'); select undef,undef,undef,.25; }
print $log "done\n"; sleep 3600 while 1;
'''
LISTEN = r'''
use Socket qw(AF_INET AF_INET6 SOCK_STREAM IPPROTO_TCP inet_pton pack_sockaddr_in pack_sockaddr_in6);
my ($ip,$port)=@ARGV; my $v6=index($ip,':')>=0; my $family=$v6?AF_INET6:AF_INET;
socket(my $s,$family,SOCK_STREAM,IPPROTO_TCP) or die $!;
my $a=inet_pton($family,$ip); bind($s,$v6?pack_sockaddr_in6($port,$a):pack_sockaddr_in($port,$a)) or die $!;
listen($s,16) or die $!; while (accept(my $c,$s)) { close $c; }
'''


def run(args, check=True, timeout=90):
    result = subprocess.run(list(map(str,args)), capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[:4]}: {result.stderr.strip()} {result.stdout.strip()}')
    return result


def host_tcp(host, port):
    try:
        with socket.create_connection((host,port), timeout=2):
            return 'connected'
    except OSError as error:
        return str(error)


def tcp(command, host, port):
    family = '6' if ':' in host else '4'
    p = run([*command,'/usr/bin/perl','-e',TCP_PROBE,host,str(port),family,'3'],check=False)
    expected = {(0,'MYBOX_TCP connected'):'connected',(10,'MYBOX_TCP timeout'):'timeout'}
    return expected.get((p.returncode,p.stdout.strip()), p.stderr.strip() or f'exit {p.returncode}')


def physical_router6(interface):
    route = run(['/sbin/route','-n','get','-inet6','default']).stdout
    linklocal = re.search(r'gateway:\s+(\S+)',route).group(1)
    neighbors = [line.split() for line in run(['/usr/sbin/ndp','-an']).stdout.splitlines()]
    mac = next(row[1] for row in neighbors if len(row)>3 and row[0]==linklocal and row[2]==interface)
    return next(row[0] for row in neighbors if len(row)>3 and row[1]==mac and row[2]==interface
                and '%' not in row[0] and ipaddress.IPv6Address(row[0]).is_global)


def inspect_box(name):
    return json.loads(run([*PRIVATE,'inspect','mybox-'+name]).stdout)[0]['status']['networks'][0]


def present(name, network=False):
    cmd = ['network','list'] if network else ['list','--all']
    return any(row.get('id',row.get('configuration',{}).get('id'))==name
               for row in json.loads(run([*PRIVATE,*cmd,'--format','json']).stdout))


def wait_for(test, seconds=15):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if test():return True
        time.sleep(.2)
    return False


def capture_discovery(bridge, guest, directory):
    """On failure, watch discovery on the disposable bridge while Linux asks for its router."""
    solicit = r'''use Socket qw(AF_INET6 SOCK_RAW IPPROTO_ICMPV6 IPPROTO_IPV6 IPV6_MULTICAST_HOPS inet_pton pack_sockaddr_in6);
open my $f, '<', '/sys/class/net/eth0/ifindex' or die $!; my $index = <$f>; chomp $index;
socket(my $s,AF_INET6,SOCK_RAW,IPPROTO_ICMPV6) or die $!;
setsockopt($s,IPPROTO_IPV6,IPV6_MULTICAST_HOPS,pack('i',255)) or die $!;
send($s,pack('CCnN',133,0,0,0),0,pack_sockaddr_in6(0,inet_pton(AF_INET6,'ff02::2'),$index)) or die $!;
'''
    capture = directory / 'discovery'
    errors = directory / 'discovery-errors'
    with capture.open('wb') as output, errors.open('wb') as error:
        proc = subprocess.Popen(['/usr/bin/sudo', '-n', '/usr/sbin/tcpdump', '-nn', '-l', '-v', '-i', bridge,
                                 'icmp6 or (ip6 and host 2606:4700:4700::1111)'],
                                stdin=subprocess.DEVNULL, stdout=output, stderr=error)
        try:
            if not wait_for(lambda: b'listening on' in errors.read_bytes(), 5):
                return {'capture_error': errors.read_text()}
            sent = run([*guest, '/usr/bin/perl', '-e', solicit], check=False)
            time.sleep(3)
            public = tcp(guest, '2606:4700:4700::1111', 443)
            guest_state = run([*guest, '/bin/cat', '/proc/sys/net/ipv6/conf/eth0/accept_ra',
                               '/proc/sys/net/ipv6/conf/eth0/forwarding', '/proc/net/ipv6_route']).stdout
        finally:
            if proc.poll() is None:
                run(['/usr/bin/sudo', '-n', '/bin/kill', '-TERM', str(proc.pid)], check=False)
            proc.wait(timeout=10)
    return {'solicitation_exit': sent.returncode, 'solicitation_error': sent.stderr.strip(),
            'public_tcp_after_solicitation': public, 'guest_state': guest_state, 'capture': capture.read_text()}


def main():
    suffix=uuid.uuid4().hex[:10]
    primary='v6-'+suffix
    peer='peer-'+suffix
    guest=[BOX,'enter',primary,'--']
    peer_guest=[*PRIVATE,'exec','mybox-'+peer]
    record={'started':datetime.now(timezone.utc).isoformat(),'probes':{},'cleanup':{}}
    fixtures=[]
    diagnostics=Path('/Library/Logs/DiagnosticReports')
    initial=set(diagnostics.glob('InternetSharing*.ips'))
    def save():
        with tempfile.NamedTemporaryFile('w',dir=REPORT.parent,delete=False) as f:
            json.dump(record,f,indent=2); f.write('\n')
        Path(f.name).replace(REPORT)
    def result(key,value):
        record['probes'][key]=value
        print(key,value,flush=True);save()
    def check_tcp(key,host,port,wanted,control=None):
        control=control or (lambda:host_tcp(host,port))
        before=control(); actual=tcp(guest,host,port); after=control()
        result(key,dict(before=before,guest=actual,after=after,
                        passed=before==after=='connected' and actual==wanted))
    def check_public_internet(stage):
        for family, host in [('ipv4', '1.1.1.1'), ('ipv6', '2606:4700:4700::1111')]:
            check_tcp('public_tcp_' + family + stage, host, 443, 'connected')
            args = ['/usr/bin/perl', '-e', UDP_DNS, host]
            control = run(args, check=False)
            box = run([*guest, *args], check=False)
            result('public_udp_dns_' + family + stage, dict(control=(control.stdout + control.stderr).strip(),
                   guest=(box.stdout + box.stderr).strip(), passed=box.returncode == control.returncode == 0))
    with ExitStack() as stack:
        try:
            run(['/usr/bin/sudo','-n','true'])
            run([BOX,'list'])
            record['version']=run([CLI,'--version']).stdout.strip()
            for n in [primary,peer]:
                if present('mybox-'+n) or present('mybox-net-'+n,True):raise RuntimeError('Test name collision.')
            config=lan_configuration()
            router4=config['address']; router6=physical_router6(config['interface'])
            if host_tcp(router4,443)!='connected' or host_tcp(router6,443)!='connected':
                raise RuntimeError('Cannot confirm both local router listeners.')
            print('Verified the IPv4 and IPv6 router listeners.',flush=True)
            listener=stack.enter_context(socket.socket());listener.bind(('0.0.0.0',0));listener.listen(24)
            listener6=stack.enter_context(socket.socket(socket.AF_INET6,socket.SOCK_STREAM))
            listener6.bind(('::',0));listener6.listen(24)
            port=listener.getsockname()[1]
            token='mb'+suffix
            tmp=Path(stack.enter_context(tempfile.TemporaryDirectory()))
            log=stack.enter_context((tmp/'capture').open('wb'))
            errors=stack.enter_context((tmp/'errors').open('wb'))
            cap=subprocess.Popen(['/usr/bin/sudo','-n','/usr/sbin/tcpdump','-i',config['interface'],
                                  '-nn','-l','-A','-s','180',
                                  f'udp and dst port {port} and (dst host {router4} or dst host {router6})'],
                                 stdin=subprocess.DEVNULL,stdout=log,stderr=errors)
            def stop_capture():
                if cap.poll() is None:
                    run(['/usr/bin/sudo','-n','/bin/kill','-TERM',str(cap.pid)],check=False)
                    cap.wait(timeout=10)
            stack.callback(stop_capture)
            def controls(prefix):
                for fam,ip,suffix_digit in [(socket.AF_INET,router4,'4'),(socket.AF_INET6,router6,'6')]:
                    with socket.socket(fam,socket.SOCK_DGRAM) as s:
                        s.sendto((token+prefix+suffix_digit).encode(),(ip,port))
                return wait_for(lambda:all((token+prefix+d).encode() in (tmp/'capture').read_bytes() for d in '46'),4)
            if not wait_for(lambda:b'listening on' in (tmp/'errors').read_bytes(),6) or not controls('before'):
                raise RuntimeError('Could not validate physical-interface capture: '+(tmp/'errors').read_text())
            print('Capture is active before Linux starts.',flush=True)
            net='mybox-net-'+primary
            fixtures.append(primary)
            run([*PRIVATE,'network','create','--label',PREFIX+'.managed=true',net])
            run([*PRIVATE,'create','--name','mybox-'+primary,'--label',PREFIX+'.managed=true','--label',
                 PREFIX+'.network='+net,'--cpus','1','--memory','512m','--dns','1.1.1.1','--network',
                 net+',alias='+primary+'.box.test','--entrypoint','/usr/bin/perl','docker.io/library/debian:trixie-slim',
                 '-e',BOOT,router4,router6,str(port),token])
            run([BOX,'start',primary],timeout=180)
            body=''
            for _ in range(14):
                body=run([*PRIVATE,'exec','mybox-'+primary,'/bin/cat','/workspace/probes'],check=False).stdout
                if 'done' in body:break
                time.sleep(2)
            after_capture=controls('after')
            captext=(tmp/'capture').read_bytes()
            for d in '46':
                key='boot'+d
                sent=key+' sent' in body
                leaked=(token+key).encode() in captext
                result(key,dict(sent=sent,arrived_on_lan=leaked,capture_controls=after_capture,
                                passed=sent and not leaked and after_capture))
            record['boot_output']=body
            stop_capture()
            # Linux can install the default route before duplicate-address detection completes.
            # Keep the first workload's observations separate from probes of a ready interface.
            def ready_ipv6():
                text = run([*PRIVATE, 'exec', 'mybox-' + primary, '/bin/cat', '/proc/net/if_inet6']).stdout
                return any(row[5] == 'eth0' and row[3] == '00' and not int(row[4], 16) & 0x48
                           for row in (line.split() for line in text.splitlines()) if len(row) == 6)
            record['ipv6_address_ready'] = wait_for(ready_ipv6, 12)
            record['accept_ra'] = run([*guest, '/bin/cat', '/proc/sys/net/ipv6/conf/eth0/accept_ra']).stdout.strip()
            record['guest_routes']=run([*guest,'/bin/cat','/proc/net/ipv6_route']).stdout
            record['guest_addresses']=run([*guest,'/bin/cat','/proc/net/if_inet6']).stdout
            check_public_internet('')
            http=run([*guest,'/usr/bin/perl','-e',PROBE,'deb.debian.org','80','4','1','8'],check=False)
            result('http_via_dns',dict(response=http.stdout.strip(),passed=http.returncode==0))
            check_tcp('physical_router_ipv4',router4,443,'timeout')
            check_tcp('physical_router_ipv6',router6,443,'timeout')
            check_tcp('mac_lan4',config['mac_address'],port,'timeout')
            attachment=inspect_box(primary)
            gateway=attachment['ipv4Gateway']
            check_tcp('mac_bridge4',gateway,port,'timeout')
            local6=host_ipv6(gateway)
            bridge=next(s.split(':',1)[0] for s in re.split(r'(?=^[^\s:]+: flags=)',run(['/sbin/ifconfig']).stdout,flags=re.M)
                        if re.search(r'\binet '+re.escape(gateway)+r'\s',s))
            if not record['probes']['public_tcp_ipv6']['passed'] or not record['probes']['public_udp_dns_ipv6']['passed']:
                record['discovery_probe'] = capture_discovery(bridge, guest, tmp)
                record['bridge_configuration'] = run(['/sbin/ifconfig', bridge]).stdout
                record['guest_attachment'] = attachment
                pf = run(['/usr/bin/sudo', '-n', '/sbin/pfctl', '-a', 'com.apple/000.mybox', '-vvsr']).stdout
                record['ipv6_pf_counters'] = [
                    rule.strip() for rule in re.split(r'(?m)^@\d+ ', pf)
                    if bridge in rule and ' inet6 ' in rule and re.search(r'Packets:\s*[1-9]', rule)
                ]
                save()
            port6=listener6.getsockname()[1]
            check_tcp('mac_linklocal6',local6,port6,'timeout',lambda:host_tcp(local6+'%'+bridge,port6))
            if set(diagnostics.glob('InternetSharing*.ips'))!=initial:
                raise RuntimeError('Apple InternetSharing recorded a crash; ending the live test.')
            fixtures.append(peer)
            run([BOX,'create',peer,'--cpus','1','--memory','512m'],timeout=180)
            other=inspect_box(peer)
            for fam in ['ipv4','ipv6']:
                ip=other[fam+'Address'].split('/')[0]
                run([*PRIVATE,'exec','--detach','mybox-'+peer,'/usr/bin/perl','-e',LISTEN,ip,str(port)])
                wait_for(lambda:tcp(peer_guest,ip,port)=='connected',6)
                check_tcp('peer_box_'+fam,ip,port,'timeout',lambda:tcp(peer_guest,ip,port))
            vpn=os.environ.get('MYBOX_VPN_HOST')
            if vpn:
                route=run(['/sbin/route','-n','get',*(['-inet6'] if ':' in vpn else []),vpn]).stdout
                if not re.search(r'interface:\s+(utun|ipsec|ppp|tun|wg)',route):
                    raise RuntimeError('The supplied VPN target does not use a tunnel interface.')
                check_tcp('real_vpn',vpn,int(os.environ['MYBOX_VPN_PORT']),'timeout')
            # Add a split route while the box is running; validate that real host TCP reaches its peer.
            vpn_ip='2001:db8:'+suffix[:4]+':'+suffix[4:8]+'::2'
            tunnel=subprocess.Popen(['/usr/bin/sudo','-n','/usr/bin/python3','-I','-B',str(ROOT/'tests/ipv6_test_tunnel.py'),vpn_ip,str(port)],
                                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            def close_tunnel():
                if not tunnel.stdin.closed:
                    tunnel.stdin.close()
                tunnel.wait(timeout=10)
            stack.callback(close_tunnel)
            with selectors.DefaultSelector() as selector:
                selector.register(tunnel.stdout,selectors.EVENT_READ)
                if not selector.select(10):raise RuntimeError('Test tunnel did not become ready.')
            interface=tunnel.stdout.readline().strip()
            if not re.fullmatch(r'utun\d+',interface) or not wait_for(lambda:host_tcp(vpn_ip,port)=='connected',8):
                raise RuntimeError('Test tunnel did not pass the Mac TCP control.')
            record['test_tunnel'] = interface
            public6 = '2606:4700:4700::1111'
            lookup = ['/sbin/route', '-n', 'get', '-inet6']
            scoped = run([*lookup, '-ifscope', interface, public6]).stdout
            ordinary = run([*lookup, public6]).stdout
            def route_interface(route):
                match = re.search(r'interface:\s+(\S+)', route)
                return match.group(1) if match else None
            scoped_interface, ordinary_interface = route_interface(scoped), route_interface(ordinary)
            result('public_ipv6_test_route', dict(scoped=scoped_interface, ordinary=ordinary_interface,
                   passed=scoped_interface == interface and ordinary_interface not in (None, interface)))
            check_public_internet('_while_tunnel_exists')
            # The peer uses a documentation address. Require evidence that PF handled
            # the packet so a timeout in the upstream network cannot pass this test.
            run([*guest, '/bin/true'])
            def routed_drops():
                output = run(['/usr/bin/sudo', '-n', '/sbin/pfctl', '-a', 'com.apple/000.mybox', '-vvsr']).stdout
                rules = re.split(r'(?m)^@\d+ ', output)
                counts = [int(match.group(1)) for rule in rules
                          if 'label "mybox_routed"' in rule and vpn_ip in rule
                          for match in [re.search(r'Packets:\s*(\d+)', rule)] if match]
                if not counts:
                    raise RuntimeError('Cannot find a live PF rule for the test tunnel peer.')
                return sum(counts)
            before = routed_drops()
            check_tcp('temporary_vpn_route6', vpn_ip, port, 'timeout')
            after = routed_drops()
            result('vpn_rule_dropped_packets', dict(before=before, after=after, passed=after > before))
            close_tunnel()
            if not wait_for(lambda: run(['/sbin/ifconfig', interface], check=False).returncode != 0):
                raise RuntimeError('The temporary tunnel is still present after closing its socket.')
            check_public_internet('_after_tunnel_closed')
        except Exception as error:
            record['error']=str(error); print(str(error),file=sys.stderr,flush=True)
        finally:
            for n in reversed(fixtures):
                try:
                    if present('mybox-'+n):
                        run([BOX,'stop',n]);run([*PRIVATE,'delete','mybox-'+n])
                    if present('mybox-net-'+n,True):run([*PRIVATE,'network','delete','mybox-net-'+n])
                    record['cleanup'][n]=not present('mybox-'+n) and not present('mybox-net-'+n,True)
                except Exception as error:record['cleanup'][n]=str(error)
            record['new_crash_reports']=[p.name for p in set(diagnostics.glob('InternetSharing*.ips'))-initial]
            record['finished']=datetime.now(timezone.utc).isoformat();save()
    if 'test_tunnel' in record:
        interface=record['test_tunnel']
        record['test_tunnel_removed'] = run(['/sbin/ifconfig', interface],check=False).returncode != 0
    if 'real_vpn' not in record['probes']:
        record['real_vpn'] = 'No reachable VPN-only server was supplied.'
    save()
    failed=[k for k,v in record['probes'].items() if not v['passed']]
    print('Failed checks:',failed,'Report:',REPORT,flush=True)
    return int(bool(failed or record.get('error') or record['new_crash_reports'] or record.get('test_tunnel_removed') is False or not all(v is True for v in record['cleanup'].values())))


if __name__=='__main__':
    sys.exit(main())
