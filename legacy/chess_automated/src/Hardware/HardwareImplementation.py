import chess
from Hardware import HardwareInterface, SafeDecorator
from typing import List, Any, Callable
import board
import busio
import digitalio
import adafruit_tca9548a
import adafruit_character_lcd.character_lcd_i2c as character_lcd
from adafruit_ht16k33 import matrix
from adafruit_mcp230xx.mcp23017 import MCP23017


class HardwareImplementation(HardwareInterface.HardwareInterface):
    """ Interface to Hardware chessboard"""

    def __init__(self):
        """ Set up hardware connection

        Sets up connections to the following bus devices over the i2c bus:
        - Sets up a TCA9548a to handle the high traffic load on the bus
        - Sets up a 8x8 reed_matrix mapped trough 4 MCP23017
        - Sets up a 9x9 led_matrix with the interface LedWrapper which uses a HT16k33 and 1 MCP23017
        """

