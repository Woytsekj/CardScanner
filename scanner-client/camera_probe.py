#!/usr/bin/env python3
import sys
import cv2

if len(sys.argv) != 2:
    raise SystemExit("usage: camera_probe.py /dev/videoX")

path = sys.argv[1]

# Prefer V4L2 for a Linux device path, but fall back to CAP_ANY. Some OpenCV
# builds warn that V4L2 cannot capture by name even though CAP_ANY can.
cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
if not cap.isOpened():
    cap.release()
    cap = cv2.VideoCapture(path, cv2.CAP_ANY)

ok = cap.isOpened()
if ok:
    ok, _ = cap.read()
cap.release()
raise SystemExit(0 if ok else 1)
