from abc import ABC, abstractmethod
from typing import List
from enum import Enum
import chess


class Offer(Enum):
    CONTINUE = 0
    DRAW = 1
    RESIGN = 2


class HardwareInterface(ABC):
    """ Defines interface to the hardware """


