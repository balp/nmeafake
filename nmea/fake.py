# This file is Copyright (c) 2010 by the GPSD project
# BSD terms apply: see the file COPYING in the distribution root for details.
"""
gpsfake.py -- classes for creating a controlled test environment around gpsd.

The gpsfake(1) regression tester shipped with gpsd is a trivial wrapper
around this code.  For a more interesting usage example, see the
valgrind-audit script shipped with the gpsd code.

To use this code, start by instantiating a TestSession class.  Use the
prefix argument if you want to run the daemon under some kind of run-time
monitor like valgrind or gdb.  Here are some particularly useful possibilities:

valgrind --tool=memcheck --gen-suppressions=yes --leak-check=yes
    Run under Valgrind, checking for malloc errors and memory leaks.

xterm -e gdb -tui --args
    Run under gdb, controlled from a new xterm.

You can use the options argument to pass in daemon options; normally you will
use this to set the debug-logging level.

On initialization, the test object spawns an instance of gpsd with no
devices or clients attached, connected to a control socket.

TestSession has methods to attach and detch fake GPSes. The
TestSession class simulates GPS devices for you with objects composed
from a pty and a class instance that cycles sentences into the master side
from some specified logfile; gpsd reads the slave side.  A fake GPS is
identified by the string naming its slave device.

TestSession also has methods to start and end client sessions.  Daemon
responses to a client are fed to a hook function which, by default,
discards them.  You can change the hook to sys.stdout.write() to dump
responses to standard output (this is what the gpsfake executable
does) or do something more exotic. A client session is identified by a
small integer that counts the number of client session starts.

There are a couple of convenience methods.  TestSession.wait() does nothing,
allowing a specified number of seconds to elapse.  TestSession.send()
ships commands to an open client session.

TestSession does not currently capture the daemon's log output.  It is
run with -N, so the output will go to stderr (along with, for example,
Valgrind notifications).

Each FakeLogGPS instance tries to packetize the data from the logfile it
is initialized with. It uses the same packet-getter as the daeomon.

The TestSession code maintains a run queue of FakeLogGPS and gps.gs (client-
session) objects. It repeatedly cycles through the run queue.  For each
client session object in the queue, it tries to read data from gpsd.  For
each fake GPS, it sends one line of stored data.  When a fake-GPS's
go predicate becomes false, the fake GPS is removed from the run queue.

There are two ways to use this code.  The more deterministic is
non-threaded mode: set up your client sessions and fake GPS devices,
then call the run() method.  The run() method will terminate when
there are no more objects in the run queue.  Note, you must have
created at least one fake client or fake GPS before calling run(),
otherwise it will terminate immediately.

To allow for adding and removing clients while the test is running,
run in threaded mode by calling the start() method.  This simply calls
the run method in a subthread, with locking of critical regions.
"""
import sys, os, time, signal, pty, termios # fcntl, array, struct
import operator, math
import exceptions, threading, socket
import gps
import packet as sniffer

# The two magic numbers below have to be derived from observation.  If
# they're too high you'll slow the tests down a lot.  If they're too low
# you'll get random spurious regression failures that usually look
# like lines missing from the end of the test output relative to the
# check file.  These numbers might have to be adjusted upward on faster
# machines.  The need for them may be symnptomatic of race conditions
# in the pty layer or elsewhere.

# Define a per-line delay on writes so we won't spam the buffers in
# the pty layer or gpsd itself.  Removing this entirely was tried but
# caused failures under NetBSD.  Values smaller than the system timer
# tick don't make any difference here.
WRITE_PAD = 0.001

# We delay briefly after a GPS source is exhausted before removing it.
# This should give its subscribers time to get gpsd's response before
# we call the cleanup code. Note that using fractional seconds in
# CLOSE_DELAY may have no effect; Python time.time() returns a float
# value, but it is not guaranteed by Python that the C implementation
# underneath will return with precision finer than 1 second. (Linux
# and *BSD return full precision.)
CLOSE_DELAY = 1

class TestLoadError(exceptions.Exception):
    def __init__(self, msg):
        self.msg = msg

class TestLoad:
    "Digest a logfile into a list of sentences we can cycle through."
    def __init__(self, logfp, predump=False):
        self.sentences = []	# This is the interesting part
        if type(logfp) == type(""):
            logfp = open(logfp, "r");            
        self.name = logfp.name
        self.logfp = logfp
        self.predump = predump
        self.logfile = logfp.name
        self.type = None
        self.sourcetype = "pty"
        self.serial = None
        # Grab the packets
        getter = sniffer.new()
        #gps.packet.register_report(reporter)
        type_latch = None
        while True:
            (len, ptype, packet) = getter.get(logfp.fileno())
            if len <= 0:
                break
            elif ptype == sniffer.COMMENT_PACKET:
                # Some comments are magic
                if "Serial:" in packet:
                    # Change serial parameters
                    packet = packet[1:].strip()
                    try:
                        (xx, baud, params) = packet.split()
                        baud = int(baud)
                        if params[0] in ('7', '8'):
                            databits = int(params[0])
                        else:
                            raise ValueError
                        if params[1] in ('N', 'O', 'E'):
                            parity = params[1]
                        else:
                            raise ValueError
                        if params[2] in ('1', '2'):
                            stopbits = int(params[2])
                        else:
                            raise ValueError
                    except (ValueError, IndexError):
                        raise TestLoadError("bad serial-parameter spec in %s"%\
                                            logfp.name)                    
                    self.serial = (baud, databits, parity, stopbits)
                elif "UDP" in packet:
                    self.sourcetype = "UDP"
                elif "%" in packet:
                    # Pass through for later interpretation 
                    self.sentences.append(packet)
            else:
                if type_latch is None:
                    type_latch = ptype
                if self.predump:
                    print `packet`
                if not packet:
                    raise TestLoadError("zero-length packet from %s"%\
                                        logfp.name)                    
                self.sentences.append(packet)
        # Look at the first packet to grok the GPS type
        self.textual = (type_latch == sniffer.NMEA_PACKET)
        if self.textual:
            self.legend = "gpsfake: line %d: "
        else:
            self.legend = "gpsfake: packet %d"

class PacketError(exceptions.Exception):
    def __init__(self, msg):
        self.msg = msg

class GPSSimulator:
    def __init__(self, currtime, latitude=0.0, longitude=0.0, course=0, speed=1, shipplan=None):
        self.setLatLon(latitude, longitude)
        self._starttime = currtime
        self._heading = course
        self._speed = speed
        self.sourcetype = "pty"
        self.serial = None
        self._shipplan = shipplan
        self._setTime(currtime)

    def setLatLon(self, lat, lon):
        self._latitude = lat
        absLat = abs(lat)
        self._latitudeTxt = "%02d%06.3f" % (math.floor(absLat),  (absLat-math.floor(absLat)) * 60)
        self._latsign = 'N'
        if self._latitude < 0:
            self._latsign = 'S'
        self._longitude = lon
        absLon = abs(lon)
        self._longitudeTxt = "%02d%06.3f" % (math.floor(absLon),  (absLon-math.floor(absLon)) * 60)
        self._longSign = 'E'
        if self._longitude < 0:
            self._longSign = 'W'

    def _setTime(self, newtime):
        if self._shipplan:
            (self._heading, self._speed) = self._shipplan.courseAtTime(newtime - self._starttime, self)
        self._time = newtime
        postime = time.gmtime(self._time)
        self._timestr = "%02d%02d%02d.000" % (postime.tm_hour, postime.tm_min, postime.tm_sec)

    def feed(self):
        time.sleep(1.0)
        self.nextPos()
        sentance = "GPRMC,%s,A,%s,%s,%s,%s,%.2f,%.2f,280511,,,S" % (self._timestr, self._latitudeTxt, self._latsign, self._longitudeTxt, self._longSign, self._speed, self._heading)
        calc_cksum = reduce(operator.xor, (ord(s) for s in sentance), 0)
        return "$%s*%02X\r\n" % (sentance, calc_cksum)

    def nextPos(self):
        self._radiuskm = 6371
        self._radiusM = 6371 / 1.852
        self._setTime(self._time+1)
        brng = math.radians(self._heading)
        time = 1.0/3600.0
        dist = self._speed * time 
        dist_deg = dist / self._radiusM
        lat1R = math.radians(self._latitude)
        lon1R = math.radians(self._longitude)
        lat2R = math.asin( math.sin(lat1R)*math.cos(dist_deg) + math.cos(lat1R)*math.sin(dist_deg)*math.cos(brng))
        lon2R = lon1R + math.atan2(math.sin(brng)*math.sin(dist_deg)*math.cos(lat1R), math.cos(dist_deg)-math.sin(lat1R)*math.sin(lat2R))
        lon2R = (lon2R+3*math.pi) % (2*math.pi) - math.pi
        self.setLatLon( math.degrees(lat2R), math.degrees(lon2R))
    
class ShipPlan:
    def __init__(self, latitude=0.0, longitude=0.0):
        self._legs = []
        self._totalLength = 0
        self.startlatitude = latitude
        self.startlongitude = longitude

    def addLeg(self, length, course, speed):
        self._legs.append([length, course, speed])
        self._totalLength += length

    def courseAtTime(self, when, sim=None):
        when = when % self._totalLength
        if when == 0 and sim:
            sim.setLatLon(self.startlatitude, self.startlongitude)
        totalLength = 0
        for (length, course, speed) in self._legs:
            if length < 0:
                return (course,speed)
            totalLength += length
            if(when < totalLength):
                return (course,speed)
        return (course,speed)

class FakeLogGPS:
    def __init__(self, testload, progress=None):
        self.testload = testload
        self.progress = progress
        self.go_predicate = lambda: True
        self.readers = 0
        self.index = 0
        self.progress("gpsfake: %s provides %d sentences\n" % (self.testload.name, len(self.testload.sentences)))

    def feed(self):
        "Feed a line from the contents of the GPS log to the daemon."
        line = self.testload.sentences[self.index % len(self.testload.sentences)]
        if "%Delay:" in line:
            # Delay specified number of seconds
            delay = line.split()[1]
            time.sleep(int(delay))
        # self.write has to be set by the derived class
        #self.write(line)
        if self.progress:
            self.progress("gpsfake: %s feeds %d=%s\n" % (self.testload.name, len(line), `line`))
        time.sleep(WRITE_PAD)
        self.index += 1
        return line

class FakePTY:
    "A FakePTY is a pty with a test log ready to be cycled to it."
    def __init__(self, gpsSimulator,
                 speed=4800, databits=8, parity='N', stopbits=1):
        #FakeLogGPS.__init__(self, testload, progress)
        # Allow Serial: header to be overridden by explicit spped.
        self.index=0
        self._gpsSimulator = gpsSimulator
        #if self._gpsSimulator.testload.serial:
        #            (speed, databits, parity, stopbits) = self._gpsSimulator.testload.serial
        self.speed = speed
        baudrates = {
            0: termios.B0,
            50: termios.B50,
            75: termios.B75,
            110: termios.B110,
            134: termios.B134,
            150: termios.B150,
            200: termios.B200,
            300: termios.B300,
            600: termios.B600,
            1200: termios.B1200,
            1800: termios.B1800,
            2400: termios.B2400,
            4800: termios.B4800,
            9600: termios.B9600,
            19200: termios.B19200,
            38400: termios.B38400,
            57600: termios.B57600,
            115200: termios.B115200,
            230400: termios.B230400,
        }
        speed = baudrates[speed]	# Throw an error if the speed isn't legal
        (self.fd, self.slave_fd) = pty.openpty()
        self.byname = os.ttyname(self.slave_fd)
        (iflag, oflag, cflag, lflag, ispeed, ospeed, cc) = termios.tcgetattr(self.slave_fd)
        cc[termios.VMIN] = 1
        cflag &= ~(termios.PARENB | termios.PARODD | termios.CRTSCTS)
        cflag |= termios.CREAD | termios.CLOCAL
        iflag = oflag = lflag = 0
        iflag &=~ (termios.PARMRK | termios.INPCK)
        cflag &=~ (termios.CSIZE | termios.CSTOPB | termios.PARENB | termios.PARODD)
        if databits == 7:
            cflag |= termios.CS7
        else:
            cflag |= termios.CS8
        if stopbits == 2:
            cflag |= termios.CSTOPB
        if parity == 'E':
            iflag |= termios.INPCK
            cflag |= termios.PARENB
        elif parity == 'O':
            iflag |= termios.INPCK
            cflag |= termios.PARENB | termios.PARODD
        ispeed = ospeed = speed
        termios.tcsetattr(self.slave_fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])
    def read(self):
        "Discard control strings written by gpsd."
        # A tcflush implementation works on Linux but fails on OpenBSD 4.
        termios.tcflush(self.fd, termios.TCIFLUSH)
        # Alas, the FIONREAD version also works on Linux and fails on OpenBSD.
        #try:
        #    buf = array.array('i', [0])
        #    fcntl.ioctl(self.master_fd, termios.FIONREAD, buf, True)
        #    n = struct.unpack('i', buf)[0]
        #    os.read(self.master_fd, n)
        #except IOError:
        #    pass

    def write(self, line):
        os.write(self.fd, line)

    def drain(self):
        "Wait for the associated device to drain (e.g. before closing)."
        termios.tcdrain(self.fd)
    def feed(self):
        line = self._gpsSimulator.feed()
        self.write(line)
        return line

class FakeUDP(FakeLogGPS):
    "A UDP broadcaster with a test log ready to be cycled to it."
    def __init__(self, testload,
                 ipaddr, port,
                 progress=None):
        FakeLogGPS.__init__(self, testload, progress)
        self.ipaddr = ipaddr
        self.port = port
        self.byname = "udp://" + ipaddr + ":" + port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def read(self):
        "Discard control strings written by gpsd."
        pass

    def write(self, line):
        self.sock.sendto(line, (self.ipaddr, int(self.port)))

    def drain(self):
        "Wait for the associated device to drain (e.g. before closing)."
        pass	# shutdown() fails on UDP

class DaemonError(exceptions.Exception):
    def __init__(self, msg):
        self.msg = msg
    def __str__(self):
        return repr(self.msg)

class DaemonInstance:
    "Control a gpsd instance."
    def __init__(self, control_socket=None):
        self.sockfile = None
        self.pid = None
        if control_socket:
            self.control_socket = control_socket
        else:
            self.control_socket = "/tmp/gpsfake-%d.sock" % os.getpid()
        self.pidfile  = "/tmp/gpsfake_pid-%s" % os.getpid()
    def spawn(self, options, port, background=False, prefix=""):
        "Spawn a daemon instance."
        self.spawncmd = None

	# Look for gpsd in GPSD_HOME env variable
        if os.environ.get('GPSD_HOME'):
            for path in os.environ['GPSD_HOME'].split(':'):
                _spawncmd = "%s/gpsd" % path
                if os.path.isfile(_spawncmd) and os.access(_spawncmd, os.X_OK):
                    self.spawncmd = _spawncmd
                    break

	# if we could not find it yet try PATH env variable for it
        if not self.spawncmd:
            if not '/usr/sbin' in os.environ['PATH']:
                os.environ['PATH']=os.environ['PATH'] + ":/usr/sbin"
            for path in os.environ['PATH'].split(':'):
                _spawncmd = "%s/gpsd" % path
                if os.path.isfile(_spawncmd) and os.access(_spawncmd, os.X_OK):
                    self.spawncmd = _spawncmd
                    break

        if not self.spawncmd:
            raise DaemonError("Cannot execute gpsd: executable not found. Set GPSD_HOME env variable")
        # The -b option to suppress hanging on probe returns is needed to cope
        # with OpenBSD (and possibly other non-Linux systems) that don't support
        # anything we can use to implement the FakeLogGPS.read() method
        self.spawncmd += " -b -N -S %s -F %s -P %s %s" % (port, self.control_socket, self.pidfile, options)
        if prefix:
            self.spawncmd = prefix + " " + self.spawncmd.strip()
        if background:
            self.spawncmd += " &"
        status = os.system(self.spawncmd)
        if os.WIFSIGNALED(status) or os.WEXITSTATUS(status):
            raise DaemonError("daemon exited with status %d" % status)
    def wait_pid(self):
        "Wait for the daemon, get its PID and a control-socket connection."
        while True:
            try:
                fp = open(self.pidfile)
            except IOError:
                time.sleep(0.1)
                continue
            try:
                fp.seek(0)
                pidstr = fp.read()
                self.pid = int(pidstr)
            except ValueError:
                time.sleep(0.5)
                continue	# Avoid race condition -- PID not yet written
            fp.close()
            break
    def __get_control_socket(self):
        # Now we know it's running, get a connection to the control socket.
        if not os.path.exists(self.control_socket):
            return None
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
            self.sock.connect(self.control_socket)
        except socket.error:
            if self.sock:
                self.sock.close()
            self.sock = None
        return self.sock
    def is_alive(self):
        "Is the daemon still alive?"
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False
    def add_device(self, path):
        "Add a device to the daemon's internal search list."
        if self.__get_control_socket():
            self.sock.sendall("+%s\r\n\x00" % path)
            self.sock.recv(12)
            self.sock.close()
    def remove_device(self, path):
        "Remove a device from the daemon's internal search list."
        if self.__get_control_socket():
            self.sock.sendall("-%s\r\n\x00" % path)
            self.sock.recv(12)
            self.sock.close()
    def kill(self):
        "Kill the daemon instance."
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                # Raises an OSError for ESRCH when we've killed it.
                while True:
                    os.kill(self.pid, signal.SIGTERM)
                    time.sleep(0.01)
            except OSError:
                pass
            self.pid = None

class TestSessionError(exceptions.Exception):
    def __init__(self, msg):
        self.msg = msg

class TestSession:
    "Manage a session including a daemon with fake GPSes and clients."
    def __init__(self, prefix=None, port=None, options=None, verbose=0, predump=True, udp=False, simulator=False):
        "Initialize the test session by launching the daemon."
        self.prefix = prefix
        self.port = port
        self.options = options
        self.verbose = verbose
        self.predump = predump
        self.udp = udp
        self.daemon = DaemonInstance()
        self.fakegpslist = {}
        self.client_id = 0
        self.readers = 0
        self.writers = 0
        self.runqueue = []
        self.index = 0
        self._simulator = simulator
        if port:
            self.port = port
        else:
            self.port = gps.GPSD_PORT
        self.progress = lambda x: None
        self.reporter = lambda x: None
        self.default_predicate = None
        self.fd_set = []
        self.threadlock = None
    def spawn(self):
        for sig in (signal.SIGQUIT, signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda signal, frame: self.cleanup())
        self.daemon.spawn(background=True, prefix=self.prefix, port=self.port, options=self.options)
        self.daemon.wait_pid()
    def set_predicate(self, pred):
        "Set a default go predicate for the session."
        self.default_predicate = pred
    def gps_add(self, logfile, speed=19200, pred=None):
        "Add a simulated GPS being fed by the specified logfile."
        self.progress("gpsfake: gps_add(%s, %d)\n" % (logfile, speed))
        if logfile not in self.fakegpslist:
            testload = TestLoad(logfile, predump=self.predump)
            if testload.sourcetype == "UDP" or self.udp:
                newgps = FakeUDP(testload, ipaddr="127.0.0.1", port="5000",
                                   progress=self.progress)
            elif self._simulator:
                plan = ShipPlan(latitude=58.1388066666, longitude=11.83308166666 )
                plan.addLeg(length=50, course=180, speed=5.0)
                plan.addLeg(length=103, course=134, speed=8.0)
                plan.addLeg(length=40, course=107, speed=10.0)
                plan.addLeg(length=4, course=107, speed=5.0)
                plan.addLeg(length=8, course=107, speed=2.5)
                plan.addLeg(length=2, course=10, speed=0.0)
                plan.addLeg(length=54, course=289, speed=8.0)
                plan.addLeg(length=105, course=316, speed=8.0)
                plan.addLeg(length=22, course=354, speed=8.0)
                plan.addLeg(length=4, course=354, speed=4.0)
                plan.addLeg(length=2, course=354, speed=2.0)
                plan.addLeg(length=2, course=354, speed=1.0)
                plan.addLeg(length=1, course=348, speed=0.0)
                gpsSim = GPSSimulator(currtime=1330759883, shipplan=plan)
                newgps = FakePTY(gpsSim, speed=speed)
            else:
                gpsSim = FakeLogGPS(testload, progress=self.progress)
                newgps = FakePTY(gpsSim, speed=speed)
            if pred:
                newgps.go_predicate = pred
            elif self.default_predicate:
                newgps.go_predicate = self.default_predicate
            self.fakegpslist[newgps.byname] = newgps
            self.append(newgps)
            newgps.exhausted = 0
        self.daemon.add_device(newgps.byname)
        return newgps.byname
    def gps_remove(self, name):
        "Remove a simulated GPS from the daemon's search list."
        self.progress("gpsfake: gps_remove(%s)\n" % name)
        self.fakegpslist[name].drain()
        self.remove(self.fakegpslist[name])
        self.daemon.remove_device(name)
        del self.fakegpslist[name]
    def client_add(self, commands):
        "Initiate a client session and force connection to a fake GPS."
        self.progress("gpsfake: client_add()\n")
        newclient = gps.gps(port=self.port, verbose=self.verbose)
        self.append(newclient)
        newclient.id = self.client_id + 1 
        self.client_id += 1
        self.progress("gpsfake: client %d has %s\n" % (self.client_id,newclient.device))
        if commands:
            self.initialize(newclient, commands) 
        return self.client_id
    def client_remove(self, cid):
        "Terminate a client session."
        self.progress("gpsfake: client_remove(%d)\n" % cid)
        for obj in self.runqueue:
            if isinstance(obj, gps.gps) and obj.id == cid:
                self.remove(obj)
                return True
        else:
            return False
    def wait(self, seconds):
        "Wait, doing nothing."
        self.progress("gpsfake: wait(%d)\n" % seconds)
        time.sleep(seconds)
    def gather(self, seconds):
        "Wait, doing nothing but watching for sentences."
        self.progress("gpsfake: gather(%d)\n" % seconds)
        #mark = time.time()
        time.sleep(seconds)
        #if self.timings.c_recv_time <= mark:
        #    TestSessionError("no sentences received\n")
    def cleanup(self):
        "We're done, kill the daemon."
        self.progress("gpsfake: cleanup()\n")
        if self.daemon:
            self.daemon.kill()
            self.daemon = None
    def run(self):
        "Run the tests."
        try:
            self.progress("gpsfake: test loop begins\n")
            while self.daemon:
                # We have to read anything that gpsd might have tried
                # to send to the GPS here -- under OpenBSD the
                # TIOCDRAIN will hang, otherwise.
                for device in self.runqueue:
                    if isinstance(device, FakeLogGPS):
                        device.read()
                had_output = False
                chosen = self.choose()
                if isinstance(chosen, FakePTY):
                    if chosen.exhausted and (time.time() - chosen.exhausted > CLOSE_DELAY):
                        self.gps_remove(chosen.byname)
                        self.progress("gpsfake: GPS %s removed\n" % chosen.byname)
                    elif not chosen.go_predicate(chosen.index, chosen):
                        if chosen.exhausted == 0:
                            chosen.exhausted = time.time()
                            self.progress("gpsfake: GPS %s ran out of input\n" % chosen.byname)
                    else:
                        chosen.feed()
                elif isinstance(chosen, gps.gps):
                    if chosen.enqueued:
                        chosen.send(chosen.enqueued)
                        chosen.enqueued = ""
                    while chosen.waiting():
                        chosen.poll()
                        if chosen.valid & gps.PACKET_SET:
                            self.reporter(chosen.response)
                        had_output = True
                else:
                    raise TestSessionError("test object of unknown type")
                if not self.writers and not had_output:
                    self.progress("gpsfake: no writers %s and no output %s\n"
                            %(self.writers, had_output))
                    break
            self.progress("gpsfake: test loop ends\n")
        finally:
            self.cleanup()

    # All knowledge about locks and threading is below this line,
    # except for the bare fact that self.threadlock is set to None
    # in the class init method.

    def append(self, obj):
        "Add a producer or consumer to the object list."
        if self.threadlock:
            self.threadlock.acquire()
        self.runqueue.append(obj)
        if isinstance(obj, FakeLogGPS):
            self.writers += 1
        elif isinstance(obj, FakePTY):
            self.writers += 1
        elif isinstance(obj, gps.gps):
            self.readers += 1
        if self.threadlock:
            self.threadlock.release()
    def remove(self, obj):
        "Remove a producer or consumer from the object list."
        if self.threadlock:
            self.threadlock.acquire()
        self.runqueue.remove(obj)
        if isinstance(obj, FakeLogGPS):
            self.writers -= 1
        elif isinstance(obj, gps.gps):
            self.readers -= 1
        self.index = min(len(self.runqueue)-1, self.index)
        if self.threadlock:
            self.threadlock.release()
    def choose(self):
        "Atomically get the next object scheduled to do something."
        if self.threadlock:
            self.threadlock.acquire()
        chosen = self.index
        self.index += 1
        self.index %= len(self.runqueue)
        if self.threadlock:
            self.threadlock.release()
        return self.runqueue[chosen]
    def initialize(self, client, commands):
        "Arrange for client to ship specified commands when it goes active."
        client.enqueued = ""
        if not self.threadlock:
            client.send(commands)
        else:
            client.enqueued = commands
    def start(self):
        self.threadlock = threading.Lock()
        threading.Thread(target=self.run)

# End
