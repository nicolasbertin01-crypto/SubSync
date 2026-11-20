# -*- coding: utf-8 -*-
"""SubSync - restauration des visages (détection SCRFD + restauration GFPGAN/GPEN, modèles ONNX)."""
import numpy as np, cv2

FFHQ_512 = np.array([[0.37691676, 0.46864664], [0.62285697, 0.46912813], [0.50123859, 0.61331904],
                     [0.39308822, 0.72541100], [0.61150205, 0.72490465]], np.float32) * 512


def _nms(boxes, scores, thr=0.4):
    order = scores.argsort()[::-1]; keep = []
    while order.size:
        i = order[0]; keep.append(i)
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0]); yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2]); yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        area = lambda b: (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
        iou = inter / (area(boxes[i]) + area(boxes[order[1:]]) - inter + 1e-9)
        order = order[1:][iou < thr]
    return keep


class FaceRestorer:
    def __init__(self, det_path, rest_path, providers, min_size=48, score=0.55, yaw_full=0.15, yaw_max=0.35, min_eyes=36):
        import onnxruntime as ort
        so = ort.SessionOptions(); so.log_severity_level = 3
        self.det = ort.InferenceSession(det_path, so, providers=providers)
        self.rest = ort.InferenceSession(rest_path, so, providers=providers)
        self.rin = self.rest.get_inputs()[0].name
        self.min_size = min_size; self.score = score; self.yaw_full = yaw_full; self.yaw_max = yaw_max; self.min_eyes = min_eyes
        m = np.zeros((512, 512), np.float32); b = 512 // 10          # masque à bords adoucis
        m[b:-b, b:-b] = 1; self.mask = cv2.GaussianBlur(m, (0, 0), b * 0.6)

    def detect(self, rgb):
        h, w = rgb.shape[:2]; S = 640; r = min(S / w, S / h)
        nw, nh = int(w * r), int(h * r)
        img = np.zeros((S, S, 3), np.float32); img[:nh, :nw] = cv2.resize(rgb, (nw, nh)).astype(np.float32)
        x = ((img - 127.5) / 128.0).transpose(2, 0, 1)[None]
        out = self.det.run(None, {self.det.get_inputs()[0].name: x})
        B, K, Sc = [], [], []
        for i, st in enumerate((8, 16, 32)):
            n = S // st
            yy, xx = np.mgrid[:n, :n]; ctr = (np.stack([xx, yy], -1).reshape(-1, 2) * st).astype(np.float32)
            ctr = np.repeat(ctr, 2, axis=0)
            sc = out[i][:, 0]; keep = sc >= self.score
            if not keep.any(): continue
            d = out[i + 3][keep] * st; c = ctr[keep]
            B.append(np.stack([c[:, 0] - d[:, 0], c[:, 1] - d[:, 1], c[:, 0] + d[:, 2], c[:, 1] + d[:, 3]], -1))
            K.append(out[i + 6][keep].reshape(-1, 5, 2) * st + c[:, None, :]); Sc.append(sc[keep])
        if not B: return []
        B = np.concatenate(B) / r; K = np.concatenate(K) / r; Sc = np.concatenate(Sc)
        res = []
        for i in _nms(B, Sc):
            if min(B[i, 2] - B[i, 0], B[i, 3] - B[i, 1]) >= self.min_size:
                res.append((B[i], K[i], float(Sc[i])))
        return res

    def restore(self, rgb, strength=0.6):
        """Restaure les visages assez grands ; strength = part du visage restauré (0-1). Renvoie (image, nb)."""
        faces = self.detect(rgb)
        if not faces: return rgb, 0
        out = None; n = 0
        for box, kps, sc in faces:
            # visage tourné (profil, trois-quarts marqué) : les modèles ne savent restaurer que des visages
            # presque de face -> on diminue puis on supprime la retouche, sinon le visage est déformé.
            le, re_, no = kps[0], kps[1], kps[2]
            ed = float(np.linalg.norm(re_ - le))
            if ed < 1: continue
            yaw = abs(float(no[0] - (le[0] + re_[0]) / 2)) / ed
            tilt = abs(float(re_[1] - le[1])) / ed
            if yaw >= self.yaw_max or tilt > 0.6 or re_[0] <= le[0]: continue
            # visage trop petit (les modèles inventent des traits) ou tête très levée / baissée : on n'y touche pas
            if ed < self.min_eyes: continue
            ey = (le[1] + re_[1]) / 2; my = (kps[3][1] + kps[4][1]) / 2
            pitch = float(no[1] - ey) / max(1.0, float(my - ey))
            if pitch < 0.32 or pitch > 0.85: continue
            k = strength * (1.0 if yaw <= self.yaw_full else (self.yaw_max - yaw) / (self.yaw_max - self.yaw_full))
            if k <= 0.02: continue
            if out is None: out = rgb.copy()
            M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), FFHQ_512, method=cv2.LMEDS)
            if M is None: continue
            crop = cv2.warpAffine(rgb, M, (512, 512), borderMode=cv2.BORDER_REPLICATE)
            x = ((crop.astype(np.float32) / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None]
            y = self.rest.run(None, {self.rin: x})[0][0]
            y = ((np.clip(y, -1, 1) + 1) / 2 * 255).transpose(1, 2, 0)
            IM = cv2.invertAffineTransform(M)
            h, w = rgb.shape[:2]
            # on ne recolle que dans le rectangle du visage (bien plus rapide que l'image entière)
            c = np.array([[0, 0, 1], [512, 0, 1], [0, 512, 1], [512, 512, 1]], np.float32) @ IM.T
            x0, y0 = max(0, int(c[:, 0].min()) - 2), max(0, int(c[:, 1].min()) - 2)
            x1, y1 = min(w, int(c[:, 0].max()) + 3), min(h, int(c[:, 1].max()) + 3)
            if x1 <= x0 or y1 <= y0: continue
            IM2 = IM.copy(); IM2[0, 2] -= x0; IM2[1, 2] -= y0
            back = cv2.warpAffine(y, IM2, (x1 - x0, y1 - y0), borderMode=cv2.BORDER_REPLICATE)
            m = cv2.warpAffine(self.mask, IM2, (x1 - x0, y1 - y0))[..., None] * k
            roi = out[y0:y1, x0:x1].astype(np.float32)
            out[y0:y1, x0:x1] = np.clip(roi * (1 - m) + back * m + 0.5, 0, 255).astype(np.uint8); n += 1
        if out is None: return rgb, 0
        return out, n
