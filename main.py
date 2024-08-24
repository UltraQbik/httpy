"""
The mighty silly webserver written in python for no good reason
"""


import os
import ssl
import time
import socket
import brotli
import signal
import threading
from src import APIv1
from src.request import *
from src.status_code import *
from src.config import BUFFER_LENGTH
from src.minimizer import minimize_html


# path mapping
path_map = {
    "/":                    {"path": "www/index.html"},
    "/index.html":          {"path": "www/index.html"},
    "/robots.txt":          {"path": "www/robots.txt"},
    "/favicon.ico":         {"path": "www/favicon.ico"},
    "/css/styles.css":      {"path": "css/styles.css"},
    "/about":               {"path": "www/about.html"},
    "/test":                {"path": "www/test.html"},
    "/projects":            {"path": "www/projects.html"},
    "/images/*":            {"path": "www/images"},
}

# API
API_VERSIONS = {
    "APIv1":                {"supported": True}
}

# internal path map
I_PATH_MAP = {
    "/err/response":        {"path": "www/err/response.html"}
}


def init_path_map(verbose: bool = False):
    """
    Initializes * paths for 'path_map'
    """

    for key in list(path_map.keys()):
        if key[-1] == "*":
            for file in os.listdir(path_map[key]["path"]):
                new_path = f"{key[:-2]}/{file}"
                new_redirect_path = f"{path_map[key]['path']}/{file}"
                path_map[new_path] = {"path": new_redirect_path}
            path_map.pop(key)
    if verbose:
        print("LIST OF ALLOWED PATHS:")
        max_len = max([len(x) for x in path_map.keys()]) + 2
        for key, value in path_map.items():
            print(f"\t{key: <{max_len}}: {value}")
        print("END OF LIST.")


class HTTPServer:
    """
    The mightier HTTP server!
    Now uses threading
    """

    def __init__(self, *, port: int, packet_size: int = BUFFER_LENGTH):
        # SSL context
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.options &= ssl.OP_NO_SSLv3
        context.check_hostname = False
        context.load_cert_chain(
            certfile=r"C:\Certbot\live\qubane.ddns.net\fullchain.pem",  # use your own path here
            keyfile=r"C:\Certbot\live\qubane.ddns.net\privkey.pem")     # here too

        # Sockets
        self.sock: ssl.SSLSocket = context.wrap_socket(
            socket.socket(socket.AF_INET, socket.SOCK_STREAM),
            server_side=True)
        self.buf_len: int = packet_size
        self.port: int = port

        # client thread list and server thread
        self.client_threads: list[threading.Thread] = []

        # add signaling
        self.stop_event = threading.Event()
        signal.signal(signal.SIGINT, self._signal_interrupt)

    def _signal_interrupt(self, *args):
        """
        Checks for CTRL+C keyboard interrupt, to properly stop the HTTP server
        """

        # stop all threads
        self.stop_event.set()
        for thread in self.client_threads:
            thread.join()

    def start(self):
        """
        Method to start the web server
        """

        # bind and start listening to port
        self.sock.bind(('', self.port))
        self.sock.setblocking(False)
        self.sock.listen()

        # listen and respond handler
        while not self.stop_event.is_set():
            # accept new client
            client = self._accept()
            if client is None:
                break

            # create thread for new client and append it to the list
            th = threading.Thread(target=self._client_thread, args=[client])
            self.client_threads.append(th)
            th.start()

        # close server socket
        self.sock.close()

    def _client_thread(self, client: ssl.SSLSocket):
        """
        Handles getting client's requests
        :param client: client ssl socket
        """

        try:
            request = self._recv_request(client)
            if request is not None:
                self._client_request_handler(client, request)
        except ssl.SSLEOFError:
            pass  # idc
        except TimeoutError:
            print("Client timeout")
        except Exception as e:
            print(e)

        # close the connection once stop even was set or an error occurred
        client.close()

    def _client_request_handler(self, client: ssl.SSLSocket, request: Request):
        """
        Handles responses to client's requests
        :param client: client
        :param request: client's request
        """

        match request.type:
            case "GET" | "HEAD":
                response = self._handle_get(client, request)
            # case "POST":  # Not Implemented
            #     response = self._handle_post(client, request)
            case _:
                with open(I_PATH_MAP["/err/response"]["path"], "r", encoding="ascii") as file:
                    data = file.read().format(status_code=str(STATUS_CODE_NOT_FOUND)).encode("ascii")
                response = Response(data, STATUS_CODE_NOT_FOUND)

        # process header data
        compressor = None
        if response.headers.get("Content-Encoding") is None and response.compress:
            supported_compressions = [x.strip() for x in getattr(request, "Accept-Encoding", "").split(",")]
            if "br" in supported_compressions:
                response.headers["Content-Encoding"] = "br"
                compressor = brotli.Compressor()
        if response.headers.get("Connection") is None:
            response.headers["Connection"] = "close"

        # generate basic message
        message = b'HTTP/1.1 ' + response.status.__bytes__() + b'\r\n'
        for key, value in response.headers.items():
            message += f"{key}: {value}\r\n".encode("ascii")
        message += b'\r\n'

        # send message
        client.sendall(message)
        for packet in response.get_data_stream():
            data = packet
            if response.headers.get("Content-Encoding") == "br":
                # Docs for brotli (and other compressions) are bad, so that may not be the correct way of doing it
                compressor.process(data)
                data = compressor.flush()

            client.sendall(data)

            # check for stop event
            if self.stop_event.is_set():
                break

    def _handle_get(self, client: ssl.SSLSocket, request: Request) -> Response:
        """
        Handles GET / HEAD requests from a client
        """

        split_path = request.path.split("/", maxsplit=16)[1:]
        if request.path in path_map:  # assume browser
            filepath = path_map[request.path]["path"]
            with open(filepath, "rb") as file:
                data = file.read()

            if filepath[-4:] == "html":
                data = minimize_html(data)

            if request.type == "GET":
                return Response(data, STATUS_CODE_OK)
            elif request.type == "HEAD":
                return Response(b'', STATUS_CODE_OK, {"Content-Length": len(data)})
            else:
                raise TypeError("Called GET handler for non-GET request")

        elif len(split_path) >= 2 and split_path[0] in API_VERSIONS:  # assume script
            # unsupported API version
            if not API_VERSIONS[split_path[0]]:
                if request.type == "GET" or request.type == "HEAD":
                    return Response(b'', STATUS_CODE_BAD_REQUEST)
                else:
                    raise TypeError("Called GET handler for non-GET request")

            return APIv1.api_call(client, request)

        else:  # assume browser
            with open(I_PATH_MAP["/err/response"]["path"], "r", encoding="ascii") as file:
                data = file.read()
            data = data.format(status_code=str(STATUS_CODE_NOT_FOUND)).encode("ascii")
            return Response(data, STATUS_CODE_NOT_FOUND)

    def _handle_post(self, client: ssl.SSLSocket, request: Request) -> Response:
        """
        Handles POSt request from a client
        """

    def _recv_request(self, client: ssl.SSLSocket) -> Request | None:
        """
        Receive request from client
        :return: request
        :raises: anything that recv raises
        """

        buffer = bytearray()
        while not self.stop_event.is_set():
            msg = client.recv(self.buf_len)
            if len(msg) == 0:
                break
            buffer += msg
            if buffer[-4:] == b'\r\n\r\n':
                return Request.create(buffer)
        return None

    def _accept(self) -> ssl.SSLSocket | None:
        """
        socket.accept, but for more graceful closing
        """

        while not self.stop_event.is_set():
            try:
                return self.sock.accept()[0]
            except BlockingIOError:
                time.sleep(0.005)
            except ssl.SSLEOFError:
                pass  # i d c
        return None


def main():
    init_path_map(True)
    server = HTTPServer(port=13700)
    server.start()


if __name__ == '__main__':
    main()
