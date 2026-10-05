import threading
from threading import Lock
from Clients import ClientInterface
import chess
from chess import engine
from Hardware import HardwareImplementation, HardwareInterface
from time import sleep
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from copy import deepcopy
import serial
import time

ser = serial.Serial("/dev/ttyACM0", 9600, timeout=1)
SLEEP_TIME = 0.5  # Time to sleep after every scan in seconds


class HardwareClient(ClientInterface.ClientInterface):
    """ Interface to Hardware chessboard """

    # Board state
    _board_lock: Lock  # Required to access _board

    # Input output variables
    _input_playResult: Optional[chess.engine.PlayResult]
    _output_playResult: Optional[chess.engine.PlayResult]
    _playResult_lock: Lock  # Required to access input or output

    # Time display
    _white_time: Optional[timedelta]
    _black_time: Optional[timedelta]
    _last_move_made: datetime
    _time_lock: Lock  # Required to display the time

    # HardwareInterface thread variables
    _hw_thread: threading.Thread
    _hwi: HardwareInterface.HardwareInterface
    _stop_flag: bool
    _flag_lock: Lock  # Required to change flag

    def __init__(self, color: chess.Color):
        """ Creates client and starts thread to control hardware chessboard

        :param color: Weather the client is black or white
        """
        super().__init__(color)
        self._board_lock = Lock()  # Required to access _board

        # Input output variables
        self._input_playResult = None
        self._output_playResult = None
        self._playResult_lock = Lock()
        self._white_time = None
        self._black_time = None
        self._last_move_made = datetime.now()
        self._time_lock = Lock()

        # Client to the HardwareInterface
        self._hw_thread = threading.Thread(target=self._hw_control, name="Hardware thread")
        self._hwi: HardwareInterface = HardwareImplementation.HardwareImplementation()
        self._stop_flag = False
        self._flag_lock = Lock()

        self._hw_thread.start()
        
        ser.setDTR(False)
        time.sleep(1)
        ser.flushInput()
        ser.setDTR(True)
        time.sleep(2)
    
    def __del__(self):
        """" Terminates hardware thread before deleting shared resources """
        with self._flag_lock:
            self._stop_flag = True
        self._hw_thread.join()

    def get_move(self) -> chess.engine.PlayResult:
        """ Returns next move from client

        :returns: next move played by the client in the normal engine output format
        """
        while True:
            with self._playResult_lock:
                res = self._output_playResult
            if res is not None:
                self._output_playResult = None
                return res
            else:
                sleep(SLEEP_TIME)

    def set_move(self, move: chess.engine.PlayResult):
        """ Report new move to client

        :param move: reports move of opponent to client using normal engine output format
        """
        with self._time_lock:
            self._last_move_made = datetime.now()
        with self._playResult_lock:
            self._input_playResult = deepcopy(move)

    def game_is_over(self) -> bool:
        """ Checks if the game has ended

        Conditions checked:
        1.  Checkmate
        2.  Opponent resignation
        3.  Stop flag raised
        """
        result = False
        with self._board_lock:
            if self._board.is_checkmate():
                result = True
        with self._playResult_lock:
            if self._input_playResult is not None:
                if self._input_playResult.resigned:
                    result = True
        with self._flag_lock:
            if self._stop_flag:
                result = True
        return result

    def synchronize_clocks(self, clock: Optional[Tuple[timedelta, timedelta]] = None) \
            -> Optional[Tuple[timedelta, timedelta]]:
        """ Synchronize clocks between two clients

        :param clock: Input clock from the other client (white time, black time)
        :return: return clock from current client (white time, black time) if available
        """
        with self._time_lock:
            if clock is not None:
                self._white_time = clock[0]
                self._black_time = clock[1]
            if self._white_time is None or self._black_time is None:
                return None
            else:
                return deepcopy(self._white_time), deepcopy(self._black_time)

    def _hw_control(self):
        """ Controls the hardware and maintains the hardware state

        The main body of the hardware thread. The hardware thread can be in 3 states. The hardware loops
        trough these 3 states until the game is over.
        1.  Detect move player: In this case it is the clients turn to move. The hw_thread tries to
            detect a valid move from the player based on the current occupancy. Next to this the hw_thread
            will highlight any unexpected changes
        2.  Wait move opponent: In this state the client is waiting until the set_move method is called.
            The hw_thread will highlight unexpected changes.
        3.  Play move opponent. The client waits until the opponents move has been played on the board. The
            client highlights the opponents move and any unexpected changes.
        """

        while not self.game_is_over():
            print("In hw control loop")
            case = -1
            with self._board_lock:
                if self._board.turn == self.color:
                    case = 0
            if case == -1:  # If statement prevents code from requesting a second lock
                with self._playResult_lock:
                    case = 1 if self._input_playResult is None else 2

            # Switch case
            if case == 0:
                # Enter move through keyboard input
                print("detecting move from player")
                self._hw_detect_move_player()
            elif case == 1:
                print("waiting for opponent to move")
                self._hw_wait_move_opponent()
            elif case == 2:
                print("playing opponent's move")
                self._hw_play_move_opponent()
            else:
                raise Exception("internal error")

    def _hw_detect_move_player(self):
        """ Detect move from player

        We compare the expected occupancy with the actual occupancy. If two squares are different then
        we set the square where the clients piece used to be as the from square and the other square as the too
        square. Using this we create a candidate move (from_square, to_square). If this is a legal move then we
        will place this move in the output field. Note that the chess library will detect any other side effects
        from the move like captures, promotions and en passant. The internal board will be updated correctly
        and the diff will be marked on the board.
        """
        draw_offer = False
        while not self.game_is_over():

            offers = 0
            if offers is HardwareInterface.Offer.RESIGN:
                self._resigned = True
                return engine.PlayResult(None, None, resigned=True)
            if offers is HardwareInterface.Offer.DRAW:
                draw_offer = True
            
            # add user input to move + logic for promotion 
            # Detect all changed squares used to make program work in short term
            print("starting square: ")
            start = input()
            startSquare =  chess.parse_square(start)
            print("ending square: ")
            end = input()
            endSquare = chess.parse_square(end)
            command = []
            
            
            
            
            move = chess.Move(startSquare, endSquare)
            move_is_legal = move in self._board.legal_moves
            
            if move_is_legal:
                with self._board_lock:
                    
                    if self._board.piece_at(move.from_square).piece_type == chess.KNIGHT:
                        command.append('n')
                    elif self._board.piece_at(move.from_square).piece_type  == chess.KING:
                        command.append('k')
                    elif self._board.piece_at(move.from_square).piece_type  == chess.PAWN:
                        command.append('p')
                    else: command.append('_')
            
                    if self._board.piece_at(move.to_square)  == None:
                        command.append('_')
                    else: command.append('x')
                    
                    command.append(start)
                    command.append(end)
                    final_command = "".join(command)
                    print("pushed to board " + final_command)
                    ser.write(final_command.encode("utf-8"))
                    
                    self._board.push(move)
                    sleep(8)
                with self._playResult_lock:
                    print("legal move played!")
                    self._output_playResult = engine.PlayResult(move, None, draw_offered=draw_offer)
                return

            sleep(SLEEP_TIME)

    def _hw_wait_move_opponent(self):
        """ Wait for opponent move only mark changes from expected state """
        while not self.game_is_over():
            with self._playResult_lock:
                if self._input_playResult is not None:
                    return

            sleep(SLEEP_TIME)

    def _hw_play_move_opponent(self):
        """ Inform hardware of opponents move by marking from and to square + any other changes """
        with self._playResult_lock:
            # Deepcopy made to ensure thread safety
            move = deepcopy(self._input_playResult.move) if self._input_playResult.move is not None else None
            draw_offered = self._input_playResult.draw_offered
        
        #Add logic for oppenent move
        # command string to send to arduino
        command = []
        startPosition = self._input_playResult.move.uci()[:2]
        endPosition = self._input_playResult.move.uci()[2:]
        
        if self._board.piece_at(move.from_square).piece_type  == chess.KNIGHT:
            command.append('n')
        elif self._board.piece_at(move.from_square).piece_type  == chess.KING:
            command.append('k')
        else: command.append('p')
        
        print(startPosition + "    ")
        print(endPosition)
        
        
        if move is None:
            return

        with self._board_lock:
            is_capture = self._board.is_capture(move)
            
        while not self.game_is_over():
            if is_capture: # if there is a capture let terminal know
                command.append('x')
                print("piece at " + str(self._input_playResult.move) + " is being captured!")
            if self._input_playResult is not None: # if there is a move to play, play it
                if not is_capture: 
                    command.append('_')
                print("playing: ", self._input_playResult.move)
                command.append(startPosition)
                command.append(endPosition)
                final_command = "".join(command)
                print(final_command)
                
                ser.write(final_command.encode("utf-8"))
                
                self._input_playResult
                with self._board_lock:
                    self._board.push(move)
                    sleep(4)
                with self._playResult_lock:
                    self._input_playResult = None
                    return
            
        
            sleep(SLEEP_TIME)

