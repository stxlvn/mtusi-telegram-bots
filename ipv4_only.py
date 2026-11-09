import socket

import urllib3.util.connection

urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
