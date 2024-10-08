import ssl
import socket
import threading
import logging.config
from time import sleep
from src.logger import *
from src.status import *
from src.fileman import FileManager
from src.structures import unified_socket, Request, Response


class HTTPyServer:
    """
    Tiny HTTPy server
    """

    def __init__(
            self,
            port: int,
            certificate: str | None = None,
            private_key: str | None = None,
            enable_ssl: bool = False,
            allow_compression: bool = False,
            cache_everything: bool = False):
        # file manager (fileman)
        self.fileman: FileManager = FileManager(
            allow_compression=allow_compression,
            cache_everything=cache_everything,
            logger=logging.getLogger())
        self.fileman.update_paths()

        # sockets
        self.port: int = port
        self.sock: unified_socket | None = None
        self.ctx: ssl.SSLContext | None = None
        if enable_ssl:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER, check_hostname=False)
            self.ctx.load_cert_chain(certfile=certificate, keyfile=private_key)

        # signaling
        from signal import signal, SIGINT
        self._is_running: threading.Event = threading.Event()
        signal(SIGINT, self.stop)

    def _make_socket(self) -> None:
        """
        Creates / recreates the socket
        """

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        if self.ctx is None:  # context doesn't exist -> ssl is disabled
            self.sock = sock
        else:  # ssl is enabled
            self.sock = self.ctx.wrap_socket(sock, server_side=True)

    def _bind_listen(self) -> None:
        """
        Binds and listens to socket
        """

        self.sock.bind(("0.0.0.0", self.port))
        self.sock.listen()

    def start(self) -> None:
        """
        Starts the HTTPy Server
        """

        # log
        logging.info("Starting the server...")

        # make and bind server socket
        self._make_socket()
        self._bind_listen()
        self._is_running.set()

        # start server handle thread
        # maybe that will help with server dying?
        threading.Thread(target=self._server_handle).start()

        # log
        logging.info(f"Server now running on '{': '.join(map(str, self.sock.getsockname()))}'")

        # keep main thread alive
        while self._is_running.is_set():
            sleep(0.1)

    def stop(self, *args) -> None:
        """
        Stops the HTTPy server
        """

        # log
        logging.info("Shutting the server down...")

        self._is_running.clear()
        for thread in threading.enumerate():
            # skip main and daemon threads
            if thread is threading.main_thread() or thread.daemon:
                continue
            thread.join()

        # log
        logging.info("Server shutdown.")

    def _server_handle(self):
        """
        Main server handle. Handles client connections reception
        """

        while self._is_running.is_set():
            client = self._accept()
            sleep(Config.SOCKET_ACK_INTERVAL)
            if client is not None:
                threading.Thread(target=self._client_handle, args=(client,)).start()

    def _client_handle(self, client: unified_socket):
        """
        Main client handle. Handles client connection request processing
        """

        # try to fetch request
        request = self._recv(client)
        if request is None:
            return

        # get response
        if request.type == "GET":
            response = self._handle_get(request)
        else:
            response = Response(data=b'Not implemented :<', status=STATUS_CODE_NOT_IMPLEMENTED)

        # modify header to close
        response.headers["Connection"] = "close"

        # send response
        self._send(client, response)

        # close connection
        client.close()

    def _handle_get(self, request: Request) -> Response:
        """
        Handles GET requests
        :param request: client's request
        :return: response to request
        """

        # supported compressions
        encodings = [x.strip() for x in getattr(request, "Accept-Encoding", "").split(",")]

        if self.fileman.exists(request.path):
            container = self.fileman.get_container(request.path)
            headers = {"Content-Type": container.uncompressed.filetype, "Content-Length": container.uncompressed.size}
            data_stream = container.uncompressed.get_data_stream()
            if container.compressed:
                if "br" in encodings:  # brotli encoding (preferred)
                    headers["Content-Encoding"] = "br"
                    headers["Content-Length"] = container.brotli_compressed.size
                    data_stream = container.brotli_compressed.get_data_stream()
                elif "gzip" in encodings:  # gzip encoding
                    headers["Content-Encoding"] = "gzip"
                    headers["Content-Length"] = container.gzip_compressed.size
                    data_stream = container.gzip_compressed.get_data_stream()
            return Response(
                data_stream=data_stream,
                status=STATUS_CODE_OK,
                headers=headers)
        else:
            error = self.fileman.get_container("/err/response").uncompressed.get_full_data().decode("utf-8")
            error = error.format(
                status_code="404 Not Found",
                error_message=f"Page at '{request.path[1:]}' not found :<")
            return Response(
                data=error.encode("utf-8"),
                status=STATUS_CODE_NOT_FOUND)

    def _send(self, client: unified_socket, response: Response) -> None:
        """
        Send response to client.
        :param client: client connection
        :param response: response
        """

        for data in response.get_data_stream():
            sent = 0
            while sent < len(data):
                try:
                    sent += client.send(data[sent:])
                except (ssl.SSLWantWriteError, BlockingIOError):
                    sleep(Config.SOCKET_ACK_INTERVAL)
                except (ssl.SSLError, OSError):
                    return

    def _recv(self, client: unified_socket) -> Request:
        """
        Recv request from client
        :param client: client connection
        :return: request
        """

        buffer = bytearray()
        while self._is_running.is_set():
            size = len(buffer)
            try:
                buffer += client.recv(Config.SOCKET_RECV_SIZE)
            except (ssl.SSLWantReadError, BlockingIOError):
                sleep(Config.SOCKET_ACK_INTERVAL)
            except (ssl.SSLError, OSError):
                break
            if buffer[-4:] == b'\r\n\r\n':
                return Request(buffer)
            if size == len(buffer) or size > Config.HTTP_MAX_RECV_SIZE:
                break

    def _accept(self) -> unified_socket | None:
        """
        Accepts new connections
        """

        while self._is_running.is_set():
            if len(threading.enumerate()) > Config.THREADING_MAX_NUMBER:
                continue
            try:
                return self.sock.accept()[0]
            except BlockingIOError:
                sleep(Config.SOCKET_ACK_INTERVAL)
            except ssl.SSLError as e:
                if e.reason not in [
                    "HTTP_REQUEST",
                    "UNSUPPORTED_PROTOCOL",
                    "SSLV3_ALERT_CERTIFICATE_UNKNOWN",
                    "VERSION_TOO_LOW",
                    "WRONG_VERSION_NUMBER",
                    "UNEXPECTED_EOF_WHILE_READING",
                ]:
                    raise


def parse_args():
    """
    Parses terminal arguments
    :return: args
    """

    from argparse import ArgumentParser

    # parser
    _parser = ArgumentParser(
        prog="httpy",
        description="https web server")

    # add arguments
    _parser.add_argument("-p", "--port",
                         help="binding port",
                         type=int,
                         required=True)
    _parser.add_argument("-c", "--certificate",
                         help="SSL certificate (or fullchain.pem)")
    _parser.add_argument("-k", "--private-key",
                         help="SSL private key")
    _parser.add_argument("--enable-ssl",
                         help="SSL for HTTPs encrypted connection (default False)",
                         default=False,
                         action="store_true")
    _parser.add_argument("--allow-compression",
                         help="allows to compress configured files (default False)",
                         default=False,
                         action="store_true")
    _parser.add_argument("--cache-everything",
                         help="stores ALL files inside cache (including compressed ones) (default False)",
                         default=False,
                         action="store_true")
    # TODO: implement verbosity check
    # _parser.add_argument("-v", "--verbose",
    #                      help="verbose (default False)",
    #                      default=False,
    #                      action="store_true")
    # TODO: implement live update
    # _parser.add_argument("-lu", "--live-update",
    #                      help="updates files in real time (default False)",
    #                      default=False,
    #                      action="store_true")

    # parse arguments
    args = _parser.parse_args()
    if args.enable_ssl and (args.certificate is None or args.private_key is None):  # check SSL keys
        _parser.error("enabled SSL requires CERTIFICATE and PRIVATE_KEY arguments")

    # return args
    return args


def main():
    args = parse_args()
    httpy = HTTPyServer(
        port=args.port,
        certificate=getattr(args, "certificate", None),
        private_key=getattr(args, "private_key", None),
        enable_ssl=args.enable_ssl,
        allow_compression=args.allow_compression,
        cache_everything=args.cache_everything
    )
    httpy.start()


if __name__ == '__main__':
    main()
