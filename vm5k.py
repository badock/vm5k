#!/usr/bin/env python
#-*- coding: utf-8 -*-
#    
#    vm5k: Automatic deployment of virtual machine on Grid'5000  
#     Created by L. Pouilloux and M. Imbert (INRIA, 2013) 
# 
#    A great thanks to A. Lèbre and J. Pastor for alpha testing :)
#
#
import os, sys, optparse, time as T, datetime as DT, json, random
from pprint import pprint
from logging import INFO, DEBUG, WARN
from itertools import cycle
from netaddr import IPNetwork
from operator import itemgetter
import xml.etree.ElementTree as ET
import execo as EX, execo_g5k, execo_engine
from execo.log import style
from execo.config import configuration, TAKTUK, SSH, SCP, CHAINPUT
from execo.action import ActionFactory
from execo.time_utils import Timer, timedelta_to_seconds
from execo_g5k.config import g5k_configuration, default_frontend_connection_params
from execo_g5k.vmutils import *
from execo_g5k.api_utils import *
from execo_g5k.planning import *

### INITIALIZATION

## Constants
deployment_tries = 1
max_vms = 10000               # Limitations due to the number of IP address in a kavlan global
fact = ActionFactory(remote_tool = TAKTUK,
                    fileput_tool = TAKTUK,
                    fileget_tool = TAKTUK)

## Command line options 
parser = optparse.OptionParser(
            prog = style.log_header( sys.argv[0]),
            description = 'A tool to deploy and configure nodes and virtual machines '
            +'with '+style.object_repr('Debian')+' and '+style.object_repr('libvirt')+\
            '\non the '+style.log_header('Grid5000')+' platform in a '+style.object_repr('KaVLAN')+\
            '.\n\nRequire '+style.log_header('execo-2.2')+'.',
            epilog = 'Example : '+sys.argv[0]+' -n 100 will install 100 VM with the default environnements for 3h '
            )
# Resources
resources = optparse.OptionGroup(parser, style.log_header('Ressources'),
                style.emph('n_vm + walltime')+'\nperform a G5K reservation that has enough RAM for the virtual machine ;'+\
                '\n'+style.emph('n_vm + oargrid_job_id')+'\nuse an existing reservation and create the virtual machine on the hosts'+\
                '\n'+style.emph('infile + walltime')+'\ndeploy virtual machines and hosts according to a placement XML file for a given walltime'+\
                '\n'+style.emph('infile + oargrid_job_id')+'\nusing a existing reservation to deploy virtual machines and hosts according to a placement XML file'
                )                                      
resources.add_option('-n', '--n_vm',
                    dest = 'n_vm',
                    type = int,
                    help = 'number of virtual machines (%default)' )
resources.add_option('-i', '--infile',
                    dest = "infile",
                    help = 'topology file describing the placement of VM on G5K sites and clusters (%default)' )
resources.add_option('-j', '--oargrid_job_id',
                    dest = 'oargrid_job_id',
                    type = int,
                    help = 'use the hosts from a oargrid_job (%default)' )
resources.add_option('-r', '--resources', 
                    default = 'grid5000',
                    dest = 'resources',
                    help = 'list of resources (%default)')
resources.add_option('-w', '--walltime',
                    default = '3:00:00',
                    dest = 'walltime',
                    help = 'duration of your reservation (%default)' )
parser.add_option_group(resources)
# Hosts configuration
hosts = optparse.OptionGroup(parser,style.log_header('Physical hosts'))
hosts.add_option('-e', '--env_name', 
                    dest = 'env_name',
                    help = 'Kadeploy environment NAME for the physical host (%default)')
hosts.add_option('-a', '--env_file', 
                    dest = 'env_file',
                    help = 'Kadeploy environment FILE for the physical host, i.e. .env file (%default)')
hosts.add_option('-g', '--oargridsub_opts',
                    default = '-t deploy',
                    dest = 'oargridsub_opts',
                    help = 'oargribsub -t option (%default)')
hosts.add_option('--forcedeploy',
                    action = "store_true", 
                    help = 'force the deployment of the hosts')
hosts.add_option('--nodeploy',
                    action = "store_true", 
                    help = 'consider that hosts are already deployed')
parser.add_option_group(hosts)
# VMs configuration
vms = optparse.OptionGroup(parser, style.log_header('Virtual machines'))
vms.add_option('-d', '--vm_distribution',
                    default = 'distributed', 
                    dest = 'vm_distribution',
                    help = 'how to distribute the VM distributed (default) or concentrated')
vms.add_option('-f', '--vm_backing_file', 
                    dest = 'vm_backing_file',
                    default = '/grid5000/images/KVM/squeeze-x64-base.qcow2',
                    help = 'backing file for your virtual machines')
vms.add_option('-t', '--vm_template', 
                    dest = 'vm_template',
                    help = 'XML string describing the virtual machine',
                    default = '<vm mem="1024" hdd="2" cpu="1" cpuset="auto"/>')
vms.add_option('-k', '--vm_disk_location', 
                    default = 'one',
                    dest = 'vm_disk_location',
                    help = 'Where to create the qcow2: one (default) or all)')
parser.add_option_group(vms)
# Run options
run = optparse.OptionGroup(parser, style.log_header('Execution output'))
run.add_option("-v", "--verbose", 
                       action = "store_true", 
                       help = 'print debug messages')
run.add_option("-q", "--quiet", 
                       action = "store_true",
                       help = 'print only warning and error messages')
run.add_option("-o", "--outdir", 
                    dest = "outdir", 
                    default = 'vm5k_'+ T.strftime("%Y%m%d_%H%M%S_%z"),
                    help = 'where to store the vm5k files')
parser.add_option_group(run)
(options, args) = parser.parse_args()


## Start a timer
timer = Timer()
execution_time = {}
out_deploy = False
## Set log level
if options.verbose:
    logger.setLevel(DEBUG)
    logger.debug(style.user1('RUNNING IN DEBUG MODE'))
    out_deploy = True
elif options.quiet:
    logger.setLevel(WARN)
else:
    logger.setLevel(INFO)
if options.nodeploy:
    deployment_tries = 0

## Start message
logger.info(style.log_header('INITIALIZATION')+'\n\n    Starting %s to create of virtual machines on Grid5000\n', style.log_header(sys.argv[0]))
logger.info('Options\n'+'\n'.join( [ style.emph(option.ljust(20))+\
                    '= '+str(value).ljust(10) for option, value in vars(options).iteritems() if value is not None ]))
## Create output directory
try:
    os.mkdir(options.outdir)
except os.error:
    pass
## Check options consistency
if options.n_vm and options.infile:
    parser.error("options -n n_vm and -i infile are mutually exclusive, see -h for help")
if options.env_name and options.env_file:
    parser.error("options -e env_name and -a env_file are mutually exclusive, see -h for help")
if options.quiet and options.verbose:
    parser.error("options -e quiet and -a verbose are mutually exclusive, see -h for help")
if options.n_vm is None and options.oargrid_job_id is None and options.infile is None:
    parser.error('must specify one of the following options: -n '+
    style('n_vm', 'emph')+', -i '+style.emph('infile')+' or -j '+style.emph('oargrid_job_id')+' , see -h for help')
if options.infile is not None and options.oargrid_job_id is None and options.infile is None:
    parser.error('must specify one of the following options: -n '+
    style('n_vm', 'emph')+', -i '+style.emph('infile')+' or -j '+style.emph('oargrid_job_id')+' , see -h for help')


### TOPOLOGY
logger.info(style.log_header('DEPLOYMENT TOPOLOGY'))
## Check the resources
# Defining number of virtual machines
if options.infile is None:
    placement = None
    n_vm = options.n_vm
else:
    placement = ET.parse(options.infile)
    n_vm = len(placement.findall('.//vm'))
logger.info('Peforming deployment of %s virtual machines', style.emph(n_vm)  )
# Computing RAM and CPU requirements
if placement is None:
    total_mem = n_vm * int(ET.fromstring(options.vm_template).get('mem'))
    total_cpu = n_vm * int(ET.fromstring(options.vm_template).get('cpu'))
else:
    total_mem = sum([ int(vm.get('mem')) for vm in placement.findall('.//vm')])
    total_cpu = sum([ int(vm.get('cpu')) for vm in placement.findall('.//vm')])    
logger.info('Total mem: '+style.emph(str(total_mem)).rjust(18)+' Total cpu: '.ljust(15)+style.emph(str(total_cpu)).rjust(15))

# Analyzing Grid'5000 resources
resources = {}
if options.infile is not None:
    logger.info( 'Using an input file for the placement: '+ style.emph(options.infile))
    placement = ET.parse(options.infile)
    for site in placement.findall('./site'):
        resources[site.get('id')] = len(site.findall('.//host'))
        for cluster in site.findall('./cluster'):
            resources[cluster.get('id')] = len(cluster.findall('.//host'))
elif options.oargrid_job_id is None:
    for element in options.resources.split(','):
        if ':' in element:
            element_uid, n_nodes = element.split(':')
        else: 
            element_uid, n_nodes = element, 0
        resources[element_uid] = int(n_nodes)
else:
    logger.info( 'Using an existing reservation: '+ style.emph(str(options.oargrid_job_id)))

logger.info('Grid\'5000 elements: '+' '.join([ style.emph(element)+' '
               +str(n_nodes)+'  ' if n_nodes !=0 else style.emph(element) 
               for element, n_nodes in resources.iteritems()]))

        
clusters = []   
sites = []
for element in resources.iterkeys():
    if element == 'grid5000':
        sites = get_g5k_sites()
        for site in sites:
            resources[site] = 0
        clusters = get_clusters(virt = True, kavlan = True)
        for cluster in clusters:
            resources[cluster] = 0
        break;
    elif element in get_g5k_sites():
        sites.append(element)
        clusters += get_clusters(virt = True, kavlan = True, sites = [element])
    elif element in get_g5k_clusters():
        clusters.append(element)
        sites.append(get_cluster_site(element))
    else:
        logger.error('Element '+style.error(element)+' is not a Grid5000 site or cluster, abort')
        exit() 
clusters = list(set(clusters))
sites = list(set(sites))        


# Get required cluster attributes
logger.info('Retrieving cluster attributes')
clusters_attr = {}
max_vm = 0
for cluster in clusters:
    attr = get_host_attributes(cluster+'-1')
    clusters_attr[cluster] =  {'cpu': attr['architecture']['smt_size'],
                              'mem': attr['main_memory']['ram_size']/1048576 }
logger.info('cpu'+'mem'.rjust(11)+'\n'+'\n'.join( [ style.emph(cluster).ljust(16) 
                +str(attr['cpu']).rjust(30)+' '+str(attr['mem']).rjust(10) for cluster, attr in clusters_attr.iteritems() ] ) )

execution_time['1-topology'] = timer.elapsed()
logger.info(style.log_header('Done in '+str(round(execution_time['1-topology'],2))+' s\n'))

    
### GRID RESERVATION
logger.info(style.log_header('GRID RESERVATION'))
if options.oargrid_job_id is not None:   
    logger.info('Using '+style.emph(str(options.oargrid_job_id))+' job')
    oargrid_job_id = options.oargrid_job_id    
else:
# Computing planning for resources
    logger.info('No oargrid_job_id given, finding a slot that suit your need')
    starttime = T.time()
    endtime = starttime + timedelta_to_seconds(DT.timedelta(days = 5))
    planning = Planning( clusters, starttime, endtime, kavlan = True)
    planning.compute_slots(options.walltime)
    logger.debug('Slots:\n'+'\n'.join( [ style.emph(format_oar_date(slot[0])).ljust(30) +\
                    ', '.join( [ element+': '+ str(n_nodes) for element, n_nodes in slot[2].iteritems()]) 
                    for slot in planning.slots]) )
# Finding slot with enough ressources
    logger.info('Filtering slots with memory '+style.emph(total_mem)+\
                ' and more than '+style.emph(total_cpu/2)+' cpu' )
    tmp_slots = planning.slots[:]
    for slot in tmp_slots:
        slot_ram = 0
        slot_cpu = 0 
        slot_has_nodes = True
        
        for resource, n_node in slot[2].iteritems():
            if resource in clusters:
                slot_ram += n_node * clusters_attr[resource]['mem']
                slot_cpu += n_node * clusters_attr[resource]['cpu']
            resouce_node = 0
            if resources.has_key(resource) and n_node < resources[resource]:
                slot_has_nodes = False
                
        logger.debug(format_oar_date(slot[0])+' '+str(slot_ram)+' '+str(slot_cpu))
        if total_mem > slot_ram or total_cpu/2 > slot_cpu or not slot_has_nodes:
            planning.slots.remove(slot)
        
    if len(planning.slots) == 0:
        logger.error('Unable to find a slot for the resources you ask, abort ...')    
        exit()
    
    
    slots_ok = planning.find_free_slots(options.walltime, resources) 
    
    
    if len(slots_ok) > 0:
        chosen_slot = slots_ok[0]
# Distributing the hosts on the chosen slot
        cluster_nodes = {}
        for cluster in chosen_slot[2].iterkeys():
            if cluster in clusters:
                cluster_nodes[cluster] = 0 
        
        iter_cluster = cycle(cluster_nodes.iterkeys())
        cluster = iter_cluster.next()
        
        vm_ram_size = int(ET.fromstring(options.vm_template).get('mem'))
        node_ram = 0
        for i_vm in range(n_vm):
            node_ram += vm_ram_size
            if node_ram + vm_ram_size > clusters_attr[cluster]['mem']:            
                node_ram = 0
                if cluster_nodes[cluster] + 1 > chosen_slot[2][cluster]:
                    cluster = iter_cluster.next()
                cluster_nodes[cluster] += 1
                cluster = iter_cluster.next()
                while cluster_nodes[cluster] >= chosen_slot[2][cluster]:
                    cluster = iter_cluster.next()
        cluster_nodes[cluster] += 1
        
        
        for cluster in cluster_nodes.iterkeys():
            if resources.has_key(cluster):
                cluster_nodes[cluster] = max( cluster_nodes[cluster], resources[cluster])
            if resources.has_key(get_cluster_site(cluster)):  
                resources[get_cluster_site(cluster)] += cluster_nodes[cluster]  
            else:
                resources[get_cluster_site(cluster)] = cluster_nodes[cluster]
    else:
        logger.error('Unable to find a slot for the resources you ask, abort ...')
        exit()            
    logger.info('Chosen slot: '+style.emph(format_oar_date(chosen_slot[0])).ljust(30) +'\n'+\
                ', '.join( [ style.emph(element)+': '+ str(n_nodes) for element, n_nodes in chosen_slot[2].iteritems()]) )
    
    resources.update(cluster_nodes)      
    resources.update({'kavlan': chosen_slot[2]['kavlan'] })
    
    
    oargrid_job_id = create_reservation(chosen_slot[0], resources, options.walltime, auto_reservation = True)

if oargrid_job_id is None:
    logger.error('No reservation available, abort ...')
    exit()
    
jobinfo = get_oargrid_job_info(oargrid_job_id)
if jobinfo['start_date'] > T.time():
    logger.info('Job %s is scheduled for %s, waiting', style.emph(oargrid_job_id), 
            style.emph(format_oar_date(jobinfo['start_date'])) )
    if T.time() > jobinfo['start_date'] + jobinfo['walltime']:
        logger.error('Job %s is already finished, aborting', style.emph(oargrid_job_id))
        exit()
else:
    logger.info('Start date = %s', format_oar_date(jobinfo['start_date']))

wait_oargrid_job_start(oargrid_job_id)
logger.info('Job '+style.emph(str(oargrid_job_id))+' has started')    

logger.info('Retrieving the KaVLAN  ')
kavlan_id = None
subjobs = get_oargrid_job_oar_jobs(oargrid_job_id)
for subjob in subjobs:
    vlan = get_oar_job_kavlan(subjob[0], subjob[1])
    if vlan is not None: 
        kavlan_id = vlan
        kavlan_site = subjob[1]
        logger.info('%s in %s found !', kavlan_id, subjob[1])        
        break
    else:
        logger.info('%s, not found', subjob[1])
if kavlan_id is None:
    logger.error('No KaVLAN found, aborting ...')
    oargriddel(oargrid_job_id)
    exit()
    
logger.info('Retrieving the subnet from API')
equips = API.get_resource_attributes('/sites/'+kavlan_site+'/network_equipments/')
for equip in equips['items']:
    if equip.has_key('vlans') and len(equip['vlans']) >2:
        all_vlans = equip['vlans'] 
for vlan, info in all_vlans.iteritems():    
    if type(info) == type({}) and info.has_key('name') and info['name'] == 'kavlan-'+str(kavlan_id):
        addresses = info['addresses'][0]
logger.info(addresses)
logger.info('Retrieving the list of hosts ...')        
hosts = get_oargrid_job_nodes( oargrid_job_id )
hosts.sort()

if options.oargrid_job_id is not None:
    logger.info('Getting the attributes of \n%s', ", ".join( [style.host(host.address.split('.')[0]) for host in hosts] ))
    clusters = []
    for host in hosts:
        cluster = get_host_cluster(host)
        if cluster not in clusters:
            clusters.append(cluster)
    clusters_attr = {}
    for cluster in clusters:
        attr = get_host_attributes(cluster+'-1')
        clusters_attr[cluster] = {
                                   'node_flops': attr['performance']['node_flops'] if attr.has_key('performance') else 0, 
                                   'ram_size': attr['main_memory']['ram_size']/1048576,
                                   'n_cpu': attr['architecture']['smt_size'] }
        
sites = []
for cluster in clusters:
    site = get_cluster_site(cluster)
    if site not in sites:
        sites.append(site)
    
        
logger.info('Generating the IP-MAC list')

ips = IPNetwork(addresses)
vm_ip = []
for ip in ips.iter_hosts():
    if ip.words[3] != 0:
        if len(sites) == 1 and ip.words[2] > (kavlan_id-4)*64+2:
            vm_ip.append(ip)
        elif ip.words[2] >= 216:
            vm_ip.append(ip)

min_ip = vm_ip[0]
ip_mac = []
macs = []
for ip in vm_ip[0:n_vm]:
    mac = [ 0x00, 0x020, 0x4e,
        random.randint(0x00, 0xff),
        random.randint(0x00, 0xff),
        random.randint(0x00, 0xff) ]
    while mac in macs:
        mac = [ 0x00, 0x020, 0x4e,
        random.randint(0x00, 0xff),
        random.randint(0x00, 0xff),
        random.randint(0x00, 0xff) ]
    macs.append(mac)
    ip_mac.append( ( str(ip), ':'.join( map(lambda x: "%02x" % x, mac) ) ) )




if placement is not None:
    logger.info('Checking the correspondance between topology and reservation')
    tmp_hosts = map( lambda host: host.address.split('.')[0], hosts)
    for host_el in placement.findall('.//host'):
        if host_el.get('id') not in tmp_hosts:
            tmp = [h for h in tmp_hosts if host_el.get('id').split('-')[0] in h]
            host_el.attrib['id'] = tmp[0]
            tmp_hosts.remove(tmp[0])   
else:
    logger.info('No topology given, VMs will be distributed')
    vm_ram_size = int(ET.fromstring(options.vm_template).get('mem'))
    if n_vm > max_vms:
        logger.warning('Reducing the number of virtual machines to %s, due to the'+\
                     ' number of available IP in the KaVLAN global', style.report_error(max_vms) )
        n_vm = max_vms
    max_vms = min (max_vms, total_mem)
    
execution_time['2-reservation'] = timer.elapsed() - sum(execution_time.values())
logger.info(style.log_header('Done in '+str(round(execution_time['2-reservation'],2))+' s\n'))


### HOSTS CONFIGURATION
logger.info(style.log_header('HOSTS CONFIGURATION'))
if options.env_name is None:
    options.env_name = 'wheezy-x64-base'
if options.env_file is not None:
    setup = Virsh_Deployment( hosts, kavlan = kavlan_id, env_file = options.env_file, outdir = options.outdir) 
else:
    setup = Virsh_Deployment( hosts, kavlan = kavlan_id, env_name = options.env_name,  outdir = options.outdir)

setup.fact = fact
setup.deploy_hosts(max_tries = deployment_tries, check_deployed_command = not options.forcedeploy, 
                   out = out_deploy)
if len(setup.hosts) == 0:
    logger.error('No hosts have been deployed, aborting')
    exit()
setup.get_hosts_attr()
max_vms = setup.get_max_vms(options.vm_template)-len(setup.hosts)

n_vm = min(n_vm, max_vms)

logger.info('Copying ssh keys')
ssh_key = '~/.ssh/id_rsa' 


EX.Put( setup.hosts, [ssh_key, ssh_key+'.pub'], remote_location='.ssh/',
        connection_params ={'user': 'root'}).run()
configure_taktuk = setup.fact.get_remote(' echo "Host *" >> /root/.ssh/config ; echo " StrictHostKeyChecking no" >> /root/.ssh/config; ',
                setup.hosts, connection_params ={'user': 'root'}).run()


if options.env_file is None:
    setup.configure_apt( )
    setup.upgrade_hosts()   
    setup.install_packages()
    setup.reboot_nodes()
else:
    logger.warning('WARNING, your environnment need to have a libvirt version > 1.0.5')    
setup.configure_libvirt(n_vm)

setup.create_disk_image(disk_image = options.vm_backing_file)
setup.ssh_keys_on_vmbase()

dhcp_hosts = ''
for ip, mac in ip_mac:    
    dhcp_hosts += 'dhcp-host='+':'+mac+','+str(ip)+'\n'
network = str(min(vm_ip))+','+str(max(vm_ip))+','+str(ips.netmask)
dhcp_range = 'dhcp-range='+network+',12h\n'


dhcp_router = 'dhcp-option=option:router,'+str(max(vm_ip))+'\n'
setup.ip_mac = ip_mac
setup.configure_service_node(dhcp_range, dhcp_router, dhcp_hosts)

execution_time['4-hosts'] = timer.elapsed() - sum(execution_time.values())
logger.info(style.log_header('Done in '+str(round(execution_time['4-hosts'],2))+' s\n'))



### VIRTUAL MACHINES CONFIGURATION
logger.info(style.log_header('VIRTUAL MACHINES'))
logger.info('Destroying VMS')
destroy_vms(setup.hosts)


if options.infile is None:    
    logger.info('No topology given, defining and distributing the VM')
    cpuset = ET.fromstring(options.vm_template).get('cpuset')
    cpusets = {'vm-'+str(i_vm): cpuset for i_vm in range(n_vm)}
    vms = define_vms(n_vm, ip_mac, mem_size = vm_ram_size, cpusets = cpusets)
    vms = setup.distribute_vms(vms, mode = options.vm_distribution)
else:
    logger.info('Distributing the virtual machines according to the topology file')
    vms = setup.distribute_vms(vms, placement = placement)
    
if options.vm_disk_location == 'one':
    logger.info('Creating disks on one hosts')
    create = create_disks(vms).run()
elif options.vm_disk_location == 'all':
    logger.info('Creating disks on all hosts')
    create = create_disks_on_hosts(vms, setup.hosts).run()
    
logger.info('Installing the VMs')
install = install_vms(vms).run()
logger.info('Starting the VMs')
start = start_vms(vms).run()
logger.info('Waiting for all VMs to have started')
wait_vms_have_started(vms, setup.service_node)

setup.write_placement_file(vms)


rows, columns = os.popen('stty size', 'r').read().split()
total_time = sum(execution_time.values())
total_space = 0
log = 'vm5k successfully executed:'
for step, exec_time in execution_time.iteritems():
    step_size = int(exec_time*int(columns)/total_time)

    log += '\n'+''.join([' ' for i in range(total_space)])+''.join(['X' for i in range(step_size)])
    total_space += int(exec_time*int(columns)/total_time)
logger.info(log)     
 
 