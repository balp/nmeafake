#!/usr/bin/env python
#
#
""" Test the nmea.GPSSimulator """

import nmea.fake
import unittest

rmcdoc = """
=== RMC - Recommended Minimum Navigation Information ===

------------------------------------------------------------------------------
                                                          12
        1         2 3       4 5        6  7   8   9    10 11|  13
        |         | |       | |        |  |   |   |    |  | |   |
 $--RMC,hhmmss.ss,A,llll.ll,a,yyyyy.yy,a,x.x,x.x,xxxx,x.x,a,m,*hh<CR><LF>
------------------------------------------------------------------------------

Field Number:

1. UTC Time
2. Status, V=Navigation receiver warning A=Valid
3. Latitude
4. N or S
5. Longitude
6. E or W
7. Speed over ground, knots
8. Track made good, degrees true
9. Date, ddmmyy
10. Magnetic Variation, degrees
11. E or W
12. FAA mode indicator (NMEA 2.3 and later)
13. Checksum

A status of V means the GPS has a valid fix that is below an internal
quality threshold, e.g. because the dilution of precision is too high 
or an elevation mask test failed.
"""

class TestGPSSimulator(unittest.TestCase):
    def testNewInstance(self):
        """ 057 42.4338 N 011 41.7128 E'
            57.70723N 11.695213333333333E
        """
        dut = nmea.fake.GPSSimulator(currtime=1330759882.338417, latitude=57.70723, longitude=11.695213333333333)
        self.assertEquals("$GPRMC,073123.000,A,5742.434,N,1141.713,E,1.00,0.00,280511,,,S*41\r\n", dut.feed())
        
    def testMove(self):
        dut = nmea.fake.GPSSimulator(currtime=1330759883, latitude=57.70723, longitude=11.695213333333333)
        self.assertEquals("$GPRMC,073124.000,A,5742.434,N,1141.713,E,1.00,0.00,280511,,,S*46\r\n", dut.feed())

    def testMove30KnotsNorth(self):
        dut = nmea.fake.GPSSimulator(currtime=1330759883, latitude=57.70723, longitude=11.695213333333333, speed=30)
        dut.nextPos()
        dut.nextPos()
        dut.nextPos()
        dut.nextPos()
#self.assertEquals(57.70723, dut._latitude)
        self.assertEquals(11.695213333333289, dut._longitude)
        self.assertEquals("$GPRMC,073128.000,A,5742.446,N,1141.713,E,30.00,0.00,280511,,,S*7D\r\n", dut.feed())

    def testMove30KnotsWest(self):
        dut = nmea.fake.GPSSimulator(currtime=1330759883, latitude=57.70723, longitude=11.695213333333333, speed=30, course=270.0)
        dut.nextPos()
        dut.nextPos()
        dut.nextPos()
        dut.nextPos()
#self.assertEquals(57.70723, dut._latitude)
        self.assertEquals("$GPRMC,073128.000,A,5742.434,N,1141.690,E,30.00,270.00,280511,,,S*77\r\n", dut.feed())


if __name__ == "__main__":
    unittest.main()
