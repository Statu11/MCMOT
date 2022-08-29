import argparse
from pathlib import Path
import cv2
import numpy as np
import pandas as pd
import os
from tqdm  import tqdm
import json
import shutil


base_path = Path("../dataset/test")
with open("ground_truth_PETS09-S2L1.txt", "w+") as f:
    for gt_path in sorted(base_path.glob("*/*/gt/gt.txt")):
        camera_id = int(gt_path.parent.parent.stem.split("c")[-1])
        df = pd.read_csv(gt_path)
        df.columns = ['fid', 'tag', 'x', 'y', 'w', 'h', 's', '1', '2', '3']
        for index, row in df.iterrows():
            # [frame, ID, left, top, width, height, 1, -1, -1, -1]
            # 〈camera_id〉 〈obj_id〉 〈frame_id〉 〈xmin〉 〈ymin〉 〈width〉 〈height〉 〈xworld〉 〈yworld〉
            frame_id = row["fid"]
            obj_id = row["tag"]
            xmin = row["x"]
            ymin = row["y"]
            width = row["w"]
            height = row["h"]
            line = str(camera_id)+" "+str(obj_id)+" "+str(frame_id)+" "+str(xmin)+" "+str(ymin)+" "+str(width)+" "+str(height)+ \
                    " " + "-1" + " " + "-1" + "\n"
            f.write(line)