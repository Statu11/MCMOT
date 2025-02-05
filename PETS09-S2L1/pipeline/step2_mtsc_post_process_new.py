import os, cv2, math
import re
import multiprocessing as mp
import numpy as np
import argparse
from tqdm          import tqdm
from utils         import init_path, check_setting
from utils.objects import Track, TrackOri
from utils.utils   import getdistance, compute_iou
from utils.zone_intra import zone

init_path()

from config import cfg

check_setting(cfg)

INPUT_DIR   = cfg.PATH.INPUT_PATH
NUM_WORKERS = int(mp.cpu_count()/2)
write_lock  = mp.Lock()

SHORT_TRACK_TH = 2
IOU_TH = 0.05
SIZE_TH = 500 #500

WITH_SU     = cfg.WITH_SU
WITHOUT_FEATURE = False

def halfway_appear(track, roi):
    side_th = 100
    h, w, _ = roi.shape
    bx = track.box_list[0]
    c_x, c_y = [int(float(bx[0])) + int(float(bx[2])/2), int(float(bx[1])) + int(float(bx[3])/2)]
    con1 = bx[0] > side_th
    con2 = bx[1] > side_th
    con3 = bx[0] + bx[2] < w - side_th
    con4 = bx[0] + bx[2] < w - side_th
    try:
        con5 = roi[c_y][c_x][0] > 128
    except:
        return False
    
    if con1 and con2 and con3 and con4 and con5:
        return True
    else:
        return False

def halfway_lost(track, roi):
    side_th = 100
    h, w, _ = roi.shape
    bx = track.box_list[-1]
    c_x, c_y = [int(float(bx[0])) + int(float(bx[2])/2), int(float(bx[1])) + int(float(bx[3])/2)]
    con1 = bx[0] > side_th
    con2 = bx[1] > side_th
    con3 = bx[0] + bx[2] < w - side_th
    con4 = bx[1] + bx[3] < h - side_th
    try:
        con5 = roi[c_y][c_x][0] > 128
    except:
        return False

    if con1 and con2 and con3 and con4 and con5:
        return True
    else:
        return False

def sort_tracks(tracks):
    sorted_tracks = dict()
    for track_id in tracks:
        track = tracks[track_id]
        track.sort()
        sorted_tracks[track_id] = track

    return sorted_tracks

def calu_track_distance(pre_tk, back_tk):
    lp = min(5, len(pre_tk)) * -1
    lb = min(5, len(back_tk))

    pre_seq = pre_tk.feature_list[lp:]
    back_seq = back_tk.feature_list[:lb]

    mindis = 999999
    for ft0 in pre_seq:
        for ft1 in back_seq:
            feature_dis_vec = ft1 - ft0
            curdis = np.dot(feature_dis_vec.T, feature_dis_vec)
            mindis = min(curdis, mindis)
            
    return mindis

def preprocess_roi(roi):
    h, w, _ = roi.shape
    width_erode = int(w * 0.1)
    height_erode = int(h * 0.1)
    roi[:, 0:width_erode, :] = 0
    roi[:, w-width_erode:w, :] = 0
    roi[0:height_erode, :, :] = 0
    roi[h-height_erode:h, :, :] = 0

    return roi

def read_features_file(file):
    camera_dir = file.split("/")[-2]
    f = open(file, "r")
    data = dict()
    data[camera_dir] = dict()

    for line in f.readlines():
        l = line.strip("\n").split(",")
        frame_id = int(l[0])
        id = int(l[1])
        box = list(map(int, list(map(float, l[2:6]))))
        if WITH_SU:
            su  = list(map(float, list(map(float, l[6:10]))))    # 加入spatial uncertainty
            features = np.array(list(map(float, l[10:])), np.float32)
        else:
            features = np.array(list(map(float, l[6:])), np.float32)
        # if box[2] * box[3] < SIZE_TH:   # 原本做法將bbox面積小於500(size_th)的bbox去除
        #     continue

        write_lock.acquire()
        if id not in data[camera_dir]:
            if WITH_SU:
                data[camera_dir][id] = Track()
            else:
                data[camera_dir][id] = TrackOri()
        track = data[camera_dir][id]
        track.feature_list.append(features)
        track.frame_list.append(frame_id)
        track.box_list.append(box)
        if WITH_SU:
            track.su_list.append(su)    # 加入spatial uncertainty
        track.id = id
        write_lock.release()

    return data

def prepare_data():
    data = dict()
    camera_dirs = list()
    for scene_dir in os.listdir(INPUT_DIR):
        if scene_dir.startswith("S0"):
            for camera_dir in os.listdir(os.path.join(INPUT_DIR, scene_dir)):
                if camera_dir.startswith("c0"):
                    camera_dirs.append(os.path.join(INPUT_DIR, scene_dir, camera_dir))
    
    files = list()
    for camera_dir in camera_dirs:
        files.append(os.path.join(camera_dir, f"{cfg.SCT}_{cfg.DETECTION}_all_features.txt"))
        
    pool = mp.Pool(NUM_WORKERS)

    for d in tqdm(pool.imap_unordered(read_features_file, files), total=len(files), desc="Loading Data"):
        data.update(d)

    pool.close()
    pool.join()

    return data, camera_dirs

def adaptation_box(tracks, resolution):
    SIDE_TH = 10
    h, w = resolution
    for track_id in tracks:
        new_box_list = list()
        track = tracks[track_id]
        for box in track.box_list:
            p0x = int(max(0, box[0] - SIDE_TH))
            p0y = int(max(0, box[1] - SIDE_TH))
            p1x = int(min(box[0] + box[2] + SIDE_TH, w-1))
            p1y = int(min(box[1] + box[3] + SIDE_TH, h-1))
            new_box = [p0x, p0y, p1x-p0x, p1y-p0y]
            new_box_list.append(new_box)
        tracks[track_id].box_list = new_box_list

    return tracks

def remove_short_track(tracks, threshold):
    delete_ids = list()
    for track_id in tracks:
        track = tracks[track_id]
        if len(track) < threshold:
            delete_ids.append(track_id)
    for track_id in delete_ids:
        tracks.pop(track_id)
    return tracks

def remove_overlapped_box(tracks, threshold):
    frame_dict = dict()
    res_tracks = dict()
    for track in tracks.values():
        for i in range(len(track)):
            fid = track.frame_list[i]
            if fid not in frame_dict:
                frame_dict[fid] = list()
            if WITH_SU:
                box = {
                    "id": track.id,
                    "gps": track.gps_list[i],
                    "feature": track.feature_list[i],
                    "box": track.box_list[i],
                    "ts": track.ts_list[i],
                    "su": track.su_list[i] 
                }
            else:
                box = {
                    "id": track.id,
                    "gps": track.gps_list[i],
                    "feature": track.feature_list[i],
                    "box": track.box_list[i],
                    "ts": track.ts_list[i] 
                }
            frame_dict[fid].append(box)

    for fid in frame_dict:
        boxes = frame_dict[fid]
        for cur_bx in boxes:
            box1 = cur_bx["box"]
            keep = True
            for cpr_bx in boxes:
                box2 = cpr_bx["box"]
                iou = compute_iou(box1, box2)
                cur_bottom = box1[1] + box1[3]
                cpr_bottom = box2[1] + box2[3]
                if iou > threshold and cur_bottom < cpr_bottom:
                    keep = False
                    break
            if keep:
                box = cur_bx["box"]
                track_id = cur_bx["id"]
                ts = cur_bx["ts"]
                fts = cur_bx["feature"]
                gps = cur_bx["gps"]
                if WITH_SU:
                    su = cur_bx["su"]
                
                if track_id not in res_tracks:
                    if WITH_SU:
                        res_tracks[track_id] = Track()
                    else:
                        res_tracks[track_id] = TrackOri()
                
                res_tracks[track_id].id = track_id
                res_tracks[track_id].frame_list.append(fid)
                res_tracks[track_id].box_list.append(box)
                res_tracks[track_id].ts_list.append(ts)
                res_tracks[track_id].gps_list.append(gps)
                res_tracks[track_id].feature_list.append(fts)
                if WITH_SU:
                     res_tracks[track_id].su_list.append(su)
                
    return res_tracks

def remove_edge_box(tracks, roi):
    side_th = 30
    h, w, _ = roi.shape
    
    for track_id in tracks:
        boxes = tracks[track_id].box_list
        l = len(boxes)
        start = 0
        for i in range(0, l):
            bx = boxes[i]
            con1 = bx[0] > side_th
            con2 = bx[1] > side_th
            con3 = bx[0] + bx[2] < w - side_th
            con4 = bx[1] + bx[3] < h - side_th

            if con1 and con2 and con3 and con4:
                break
            else:
                start = i

        end = l-1
        for i in range(l-1, -1, -1):
            bx = boxes[i]
            con1 = bx[0] > side_th
            con2 = bx[1] > side_th
            con3 = bx[0] + bx[2] < w - side_th
            con4 = bx[1] + bx[3] < h - side_th

            if con1 and con2 and con3 and con4:
                break
            else:
                end = i
        end += 1

        if start >= end:
            tracks[track_id].box_list = boxes[0:1]
            tracks[track_id].feature_list = tracks[track_id].feature_list[0:1]
            tracks[track_id].frame_list = tracks[track_id].frame_list[0:1]
            tracks[track_id].gps_list = tracks[track_id].gps_list[0:1]
            tracks[track_id].ts_list = tracks[track_id].ts_list[0:1]
            if WITH_SU:
                tracks[track_id].su_list = tracks[track_id].su_list[0:1]
        else:
            tracks[track_id].box_list = boxes[start:end]
            tracks[track_id].feature_list = tracks[track_id].feature_list[start:end]
            tracks[track_id].frame_list = tracks[ track_id].frame_list[start:end]
            tracks[track_id].gps_list = tracks[track_id].gps_list[start:end]
            tracks[track_id].ts_list = tracks[track_id].ts_list[start:end]
            if WITH_SU:
                tracks[track_id].su_list = tracks[track_id].su_list[start:end]

    return tracks

def remove_abnormal_speed_tracks(tracks):
    delete_ids = list()
    speeds = list()
    for track_id in tracks:
        track = tracks[track_id]
        speed = track.speed()
        if speed == 0:
            delete_ids.append(track_id)
            continue
        speeds.append(speed)

    for id in delete_ids:
        tracks.pop(id)

    ids = np.array(list(tracks.keys()))
    speeds = np.array(speeds)
    mean = speeds.mean()
    std = speeds.std()
    distribution = (speeds - mean) / std
    thres = 1
    while True:
        delete_ids = ids[distribution > thres]
        if float(delete_ids.shape[0]) / ids.shape[0] <= 0.03:
            break
        thres += 0.5

    for id in delete_ids.tolist():
        tracks.pop(id)
    
    return tracks

def connect_lost_tracks(tracks, roi):
    halfway_list = list()
    for track_id in tracks:
        track = tracks[track_id]
        if halfway_appear(track, roi) or halfway_lost(track, roi):
            halfway_list.append(track)

    halfway_list = sorted(halfway_list, key=lambda tk: tk.frame_list[0])
    delete_ids = list()
    for lost_tk in halfway_list:
        if lost_tk.id in delete_ids:
            continue
        for apr_tk in halfway_list:
            if apr_tk.id in delete_ids:
                continue
            if lost_tk.frame_list[-1] < apr_tk.frame_list[0]:
                dis = calu_track_distance(lost_tk, apr_tk)
                frame_dif = apr_tk.frame_list[0] - lost_tk.frame_list[-1]
                if frame_dif < 5:
                    th = 22
                else:
                    th = 8

                if dis < th:
                    for i in range(len(apr_tk)):
                        lost_tk.frame_list.append(apr_tk.frame_list[i])
                        lost_tk.feature_list.append(apr_tk.feature_list[i])
                        lost_tk.box_list.append(apr_tk.box_list[i])
                        lost_tk.gps_list.append(apr_tk.gps_list[i])
                        lost_tk.ts_list.append(apr_tk.ts_list[i])
                        if WITH_SU:
                            lost_tk.su_list.append(apr_tk.su_list[i])
                    delete_ids.append(apr_tk.id)
    
    for id in delete_ids:
        tracks.pop(id)
    
    return tracks

def remove_no_moving_tracks(tracks, iou_threshold):
    delete_ids = list()
    ids = np.array(list(tracks.keys()))
    stay_time = list()
    for track_id in ids.tolist():
        track = tracks[track_id]
        box1 = track.box_list[0]
        box2 = track.box_list[-1]
        iou = compute_iou(box1, box2)
        t1 = track.frame_list[0]
        t2 = track.frame_list[-1]
        stay_time.append(t2-t1)
        if iou > iou_threshold:
            delete_ids.append(track_id)

    stay_time = np.array(stay_time)
    mean = stay_time.mean()
    std = stay_time.std()
    distribution = (stay_time - mean) / std
    thres = 1
    while True:
        delete = ids[abs(distribution) > thres]
        if float(delete.shape[0]) / ids.shape[0] <= 0.03:
            break
        thres += 0.5
    delete_ids.extend(delete.tolist())
        
    for id in set(delete_ids):
        tracks.pop(id)
    
    return tracks

def get_zone(zone, bbox):
    cx = int((bbox[0] + (bbox[2] / 2)))
    cy = int((bbox[1] + (bbox[3] / 2)))
    pix = zone[cy, cx, :]
    zone_num = 0
    if pix[0] > 50 and pix[1] > 50 and pix[2] > 50:  # w
        zone_num = 1
    if pix[0] < 50 and pix[1] < 50 and pix[2] > 50:  # r
        zone_num = 2
    if pix[0] < 50 and pix[1] > 50 and pix[2] < 50:  # g
        zone_num = 3
    if pix[0] > 50 and pix[1] < 50 and pix[2] < 50:  # b
        zone_num = 4
    return zone_num

def is_ignore(zone_list,frame_list, cid):
    # 0 不在任何路口 1 白色 2 红色 3 绿色 4 蓝色
    zs, ze = zone_list[0], zone_list[-1]
    fs, fe = frame_list[0],frame_list[-1]
    if zs == ze:
        # 如果一直在一个区域里，排除
        if ze in [1,2]:
            return 2
        if zs!=0 and 0 in zone_list:
            return 0
        if fe-fs>1500:
            return 2
        if fs<2:
            if cid in [45]:
                if ze in [3,4]:
                    return 1
                else:
                    return 2
        if fe > 1999:
            if cid in [41]:
                if ze not in [3]:
                    return 2
                else:
                    return 0
        if fs<2 or fe>1999:
          if ze in [3,4]:
            return 0
        if ze in [3,4]:
            return 1
        return 2
    else:
        # 如果区域发生变化
        if cid in [41, 42, 43, 44, 45, 46]:
            # 如果从支路进支路出，排除
            if zs == 1 and ze == 2:
                return 2
            if zs == 2 and ze == 1:
                return 2
            if zs == 0 and ze == 2: #我加的
                return 2
            if zs == 0 and ze == 1: #我加的
                return 2
        if cid in [41]:
            # 在41相机，车辆没有进出42相机
            if (zs in [1, 2]) and ze == 4:
                return 2
            if zs == 4 and (ze in [1, 2]):
                return 2
        if cid in [46]:
            # 在46相机，车辆没有进出45相机
            if (zs in [1, 2]) and ze == 3:
                return 2
            if zs == 3 and (ze in [1, 2]):
                return 2
        return 0

def TFS_DBTM(tracks, camera_dir):
    '''
    Referece: https://github.com/LCFractal/AIC21-MTMC
    Tracklet Filter Strategy +  Direction Based Temporal Mask
    '''
    zone_path = "/home/ykwei/MCMT-SU/mcmt/pipeline/zone"
    current_cam = camera_dir.split("/")[-1]
    cid = int(current_cam[1:])
    zone = cv2.imread(os.path.join(zone_path, f"{current_cam}.png"))

    # filter by zone
    delete_ids = list()
    for track_id in tracks:
        track = tracks[track_id]
        zone_list = list()
        for box in track.box_list:
            zone_list.append(get_zone(zone, box))
        frame_list = track.frame_list
        if is_ignore(zone_list, frame_list, cid) != 0:
            delete_ids.append(track_id)
    for track_id in delete_ids:
        tracks.pop(track_id)


    return tracks

def main(_input):
    tracks, camera_dir = _input
    if camera_dir.split("/")[-1] == "c011":
        resolution = (576, 768)
    else:
        resolution = (576, 720)

    # track = TFS_DBTM(tracks, camera_dir) # for testing data
    tracks = remove_short_track(tracks, SHORT_TRACK_TH) #use
    # tracks = connect_lost_tracks(tracks, roi)
    # tracks = remove_edge_box(tracks, roi)   #use
    # tracks = remove_short_track(tracks, SHORT_TRACK_TH) #use
    # tracks = remove_overlapped_box(tracks, IOU_TH) #use
    # tracks = remove_short_track(tracks, SHORT_TRACK_TH) #use
    # tracks = remove_abnormal_speed_tracks(tracks)   #use
    # tracks = remove_no_moving_tracks(tracks, IOU_TH)    #use
    # tracks = adaptation_box(tracks, resolution) # make detection box bigger #use
    
    # WITHOUT_FEATURE，去除feature寫入檔案中，用來檢驗後處理結果使用
    # WITH_SU，SCT後的輸出檔案有沒有包含Spatial Uncertainty
    if WITHOUT_FEATURE:
        camera_id = camera_dir.split('/')[-1]
        result_file_path = os.path.join(camera_dir, f"{camera_id}_post.txt")
    else:
        result_file_path = os.path.join(camera_dir, f"{cfg.SCT}_{cfg.DETECTION}_all_features_post.txt")
    with open(result_file_path, "w") as f:
        for track in tracks.values():
            obj_id_str = str(track.id)
            for i in range(len(track)):
                if WITHOUT_FEATURE:
                    frame_id_str = str(track.frame_list[i])
                    box_str = ",".join(list(map(str, track.box_list[i])))
                    if WITH_SU:
                        su_str = ",".join(list(map(str, track.su_list[i])))
                        line = frame_id_str + ',' + obj_id_str + ',' + box_str + ',' + su_str + '\n'
                        f.write(line)
                    else:
                        line = frame_id_str + ',' + obj_id_str + ',' + box_str + '\n'
                        f.write(line)
                else:
                    frame_id_str = str(track.frame_list[i])
                    box_str = ",".join(list(map(str, track.box_list[i])))
                    feature_str = ",".join(list(map(str, track.feature_list[i])))
                    if WITH_SU:
                        su_str = ",".join(list(map(str, track.su_list[i])))
                        line = frame_id_str + ',' + obj_id_str + ',' + box_str + ',' + su_str + \
                        ',' + feature_str + '\n'
                    else:
                        line = frame_id_str + ',' + obj_id_str + ',' + box_str + \
                        ',' + feature_str + '\n'
                    f.write(line)
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--without_feature', action='store_true')
    args = parser.parse_args()

    if args.without_feature:
        WITHOUT_FEATURE = True

    data, camera_dirs = prepare_data()
    pool = mp.Pool(NUM_WORKERS)
    print (f"Create {NUM_WORKERS} processes.")
    _input = list()
    for camera_dir in camera_dirs:
        camera_id = camera_dir.split('/')[-1]
        tracks = data[camera_id]
        tracks = sort_tracks(tracks)
        _input.append([tracks, camera_dir])
    del data
    for _ in tqdm(pool.imap_unordered(main, _input), total=len(_input), desc=f"Post Processing"):
        pass
    
    pool.close()
    pool.join()
    