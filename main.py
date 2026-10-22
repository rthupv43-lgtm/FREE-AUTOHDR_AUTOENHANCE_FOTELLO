# Decompiled from AutoFotello.exe (main.pyc)

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass
import os
import subprocess
import threading
import time
import json
import hashlib
import queue
from pathlib import Path
import base64
import logging
import urllib.request
import urllib.error
import urllib.parse
import zipfile
import io
import re
import webbrowser
import platform
import shutil
from datetime import datetime


import webview
import requests
import certifi
from tls_config import configure_ca_bundle
import PIL.Image
import PIL.ExifTags
try:
    import pillow_heif
except Exception:
    pillow_heif = None
import imagehash
import rawpy
import websocket
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import uuid




configure_ca_bundle()


import app
import fotello_engine
import autohdr_engine
import upscale_engine
import autoenhance_engine
import detail_merge_engine
import storage_manager
import free_tools_engine


if __name__ == "__main__":
    app.main()
