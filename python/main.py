import os, logging
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import tensorflow as tf
tf.get_logger().setLevel(logging.ERROR)
from typing import Union
from tensorflow.keras.models import load_model
from fastapi import FastAPI, File, UploadFile, Form, Body
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict, Any
import random
import numpy as np
import pandas as pd
import pydicom
import keras_ocr
from pydicom.errors import InvalidDicomError
from PIL import Image
import cv2
import json
import os
import shutil
from glob import glob
import datetime
import time
import re
import hashlib
import base64
from segment_anything import sam_model_registry
import torch.nn as nn
import torch.nn.functional as F
from copy import deepcopy
from tiny_vit_sam import TinyViT
from segment_anything.modeling import MaskDecoder, PromptEncoder, TwoWayTransformer
import torch
from uvicorn import run

class user_options_class(BaseModel):
    clean_image: bool
    retain_safe_private: bool
    retain_uids: bool
    retain_device_identity: bool
    retain_patient_characteristics: bool
    date_processing: str
    retain_descriptors: bool
    patient_pseudo_id_prefix: str

class session_patient_instance_class(BaseModel):
    patientPseudoId: str
    daysOffset: float
    secondsOffset: int

class session_class(BaseModel):
    patients: dict[str, session_patient_instance_class]

class ResponseModel(BaseModel):
    message: str

class DicomData(BaseModel):
    pixelData: str
    filepath: str
    classes: list

class BoxData(BaseModel):
    normalizedStart: Dict
    normalizedEnd: Dict
    segClass: int
    inpIdx: int

def clean_all():
    clean_config_session()
    clean_imgs()

def clean_config_session():
    if os.path.isfile('./tmp/session-data/session.json'):
        os.remove('./tmp/session-data/session.json')
    if os.path.isfile('./tmp/session-data/requested-action-group-dcm.csv'):
        os.remove('./tmp/session-data/requested-action-group-dcm.csv')
    if os.path.isfile('./tmp/session-data/custom-config.csv'):
        os.remove('./tmp/session-data/custom-config.csv')

def clean_imgs():
    dp, _, fps = list(os.walk('./tmp/session-data/raw'))[0]
    for fp in fps:
        if fp != '.gitkeep':
            os.remove(dp + '/' + fp)
    if os.path.exists('./tmp/session-data/clean/de-identified-files'):
        shutil.rmtree('./tmp/session-data/clean/de-identified-files')
    dp, _, fps = list(os.walk('./static/client-data'))[0]
    for fp in fps:
        if fp != '.gitkeep':
            os.remove(dp + '/' + fp)

def DCM2DictMetadata(ds: pydicom.dataset.Dataset) -> dict:
    ds_metadata_dict = {}
    for ds_attr in ds:
        ds_tag_idx = re.sub('[(,) ]', '', str(ds_attr.tag))
        if ds_tag_idx == '7fe00010': continue
        if ds_attr.VR != 'SQ':
            value = str(ds_attr.value)
        else:
            value = []
            for inner_ds_idx in range(ds[ds_tag_idx].VM):
                value.append\
                (
                    DCM2DictMetadata\
                    (
                        ds = ds[ds_tag_idx][inner_ds_idx]
                    )
                )

        ds_metadata_dict[ds_tag_idx] = \
        {
            'vr': ds_attr.VR,
            'name': ds_attr.name,
            'value': value,
        }
    return ds_metadata_dict

app = FastAPI()
app.mount\
(
    path = '/static',
    app = StaticFiles(directory='static'),
    name = 'sttc'
)

@app.get('/')
async def get_root():
    clean_all()
    return FileResponse('./templates/index.html')

@app.post('/conversion_info')
async def conversion_info(dicom_pair_fp: List[str] = Body(...)):
    downscale_dimensionality = 1024
    raw_dcm = pydicom.dcmread(dicom_pair_fp[0])
    cleaned_dcm = pydicom.dcmread(dicom_pair_fp[1])
    raw_hash = hashlib.sha256(raw_dcm.pixel_array.tobytes()).hexdigest()
    raw_img_fp = './static/client-data/' + raw_hash + '.png'
    if not os.path.exists(raw_img_fp):
        raw_img = image_preprocessing(raw_dcm.pixel_array, downscale_dimensionality = downscale_dimensionality, multichannel = True, retain_aspect_ratio = True)
        Image.fromarray(raw_img).save(raw_img_fp)
    cleaned_hash = hashlib.sha256(cleaned_dcm.pixel_array.tobytes()).hexdigest()
    cleaned_img_fp = './static/client-data/' + cleaned_hash + '.png'
    if not os.path.exists(cleaned_img_fp):
        cleaned_img = image_preprocessing(cleaned_dcm.pixel_array, downscale_dimensionality = downscale_dimensionality, multichannel = True, retain_aspect_ratio = True)
        Image.fromarray(cleaned_img).save(cleaned_img_fp)
    return \
    {
        'raw_dicom_metadata': DCM2DictMetadata(ds = raw_dcm),
        'raw_dicom_img_fp': raw_img_fp,
        'cleaned_dicom_metadata': DCM2DictMetadata(ds = cleaned_dcm),
        'cleaned_dicom_img_fp': cleaned_img_fp,
    }

@app.post('/get_mask_from_file/')
async def get_mask_from_file(current_dcm_fp: str = Body(...)):
    current_dcm = pydicom.dcmread(current_dcm_fp)
    return \
    {
        'PixelData': base64.b64encode(current_dcm.SegmentSequence[0].PixelData).decode('utf-8'),
        'dimensions': [current_dcm.Columns, current_dcm.Rows]
    }

@app.post('/modify_dicom/')
async def modify_dicom(data: DicomData):
    pixelData = base64.b64decode(data.pixelData)
    filepath = data.filepath
    modified_dcm = pydicom.dcmread(filepath)
    modified_dcm.SegmentSequence[0].PixelData = pixelData
    modified_dcm.SegmentSequence[0].SegmentDescription = ';'.join(data.classes)
    modified_dcm.save_as(filepath)
    return \
    {
        'success': True
    }

@app.post('/upload_files/')
async def get_files(files: List[UploadFile] = File(...)):
    clean_imgs()
    proper_dicom_paths = []
    total_uploaded_file_bytes = 0
    for file in files:
        contents = await file.read()
        fp = './tmp/session-data/raw/' + file.filename.split('/')[-1]
        with open(file = fp, mode = 'wb') as f:
            f.write(contents)
        try:
            pydicom.dcmread(fp)
            proper_dicom_paths.append(fp)
            total_uploaded_file_bytes += len(contents)
        except InvalidDicomError:
            print('W: The following path does not correspond to a DICOM file\n%s'%(fp))
            os.remove(fp)
            print('Irrelevant file deleted')
    total_uploaded_file_megabytes = '%.1f'%(total_uploaded_file_bytes / (10**3)**2)
    return {'n_uploaded_files': len(proper_dicom_paths), 'total_size': total_uploaded_file_megabytes}

@app.post('/correct_seg_homogeneity')
async def correct_seg_homogeneity():
    def SegmentSequenceHomogeneityCheck(fps: list[str]) -> bool:
        found_classes = []
        for fp in fps:
            dcm = pydicom.dcmread(fp)
            try:
                mask = np.frombuffer(dcm.SegmentSequence[0].PixelData, dtype = np.uint8).reshape((dcm.Rows, dcm.Columns))
                found_classes.append(dcm.SegmentSequence[0].SegmentDescription)

                if len(found_classes) != (len(np.unique(mask))):
                    return False
            except:
                return False
        if len(set(found_classes)) > 1 or dcm.SegmentSequence[0].SegmentDescription.split(';')[0] != 'background':
            return False
        return True
    with open(file = './tmp/session-data/user-options.json', mode = 'r') as file:
        user_input = json.load(file)
    fps = glob(os.path.join(user_input['output_dcm_dp'], '**', '*.dcm'), recursive = True)
    homogeneity_state = SegmentSequenceHomogeneityCheck(fps)
    if not homogeneity_state:
        print('W: Input segment sequence attributes are not homogeneous on input batch')
        renew_segm_seq(fps, ['background'])

@app.post('/get_batch_classes')
async def get_batch_classes():
    with open(file = './tmp/session-data/user-options.json', mode = 'r') as file:
        user_input = json.load(file)
    fps = glob(os.path.join(user_input['output_dcm_dp'], '**', '*.dcm'), recursive = True)
    try:
        found_classes = {'classes': pydicom.dcmread(fps[0]).SegmentSequence[0].SegmentDescription.split(';')}
    except:
        exit('E: Fatal Error; corrupted segmentation sequence attribute detected')
    return found_classes

@app.post('/align_classes')
async def align_classes(classes: List[str]):
    with open(file = './tmp/session-data/user-options.json', mode = 'r') as file:
        user_input = json.load(file)
    fps = glob(os.path.join(user_input['output_dcm_dp'], '**', '*.dcm'), recursive = True)
    renew_segm_seq(fps, classes)

def renew_segm_seq(fps: list[str], classes: list[str]):
    if classes != ['background']:
        print('W: Proceeding to overwrite the batch DICOM headers with a segmentation sequence that adheres to newly defined classes; the segmentation masks are reset')
    else:
        print('W: Proceeding to overwrite the batch DICOM headers with a segmentation sequence containing only the background; the segmentation masks are reset')
    for fp in fps:
        dcm = pydicom.dcmread(fp)
        img_shape = dcm.pixel_array.shape
        mask = np.zeros(shape = img_shape, dtype = np.uint8)
        dcm = attach_segm_data(dcm = dcm, seg_mask = mask, class_names = classes)
        dcm.save_as(fp)

@app.post('/session')
async def handle_session_button_click(session_dict: Dict[str, Any]):
    with open(file = './tmp/session-data/session.json', mode = 'w') as file:
        json.dump(session_dict, file)

@app.post("/custom_config/")
async def get_files(ConfigFile: UploadFile = File(...)):
    contents = await ConfigFile.read()
    with open(file = './tmp/session-data/custom-config.csv', mode = 'wb') as file:
        file.write(contents)

@app.post("/medsam_estimation/")
async def medsam_estimation(boxdata: BoxData):
    start = boxdata.normalizedStart
    end = boxdata.normalizedEnd
    segClass = boxdata.segClass
    inpIdx = boxdata.inpIdx
    bbox = np.array([min(start['x'],end['x']), min(start['y'],end['y']), max(end['x'],start['x']), max(end['y'], start['y'])])
    box_256 = bbox[None, :] * 256
    print('Starting segmentation')
    t0 = time.time()
    medsam_seg = medsam_inference(medsam_model, embeddings[inpIdx], box_256, (newh, neww), (Hs[inpIdx], Ws[inpIdx]))
    medsam_seg = (segClass * medsam_seg).astype(np.uint8)
    print('Segmentation completed in %.2f seconds'%(time.time()-t0))
    return \
    {
        'mask': base64.b64encode(medsam_seg).decode('utf-8'),
        'dimensions': [Ws[inpIdx], Hs[inpIdx]]
    }

@app.post('/submit_button')
async def handle_submit_button_click(user_options: user_options_class):
    user_options = dict(user_options)
    dp, _, fps = list(os.walk('./tmp/session-data/raw'))[0]
    if set(fps).issubset({'.gitkeep'}):
        return False
    default_options = \
    {
        "input_dcm_dp": "./tmp/session-data/raw",
        "output_dcm_dp": "./tmp/session-data/clean",
        "clean_image": True,
        "retain_safe_private": False,
        "retain_uids": False,
        "retain_device_identity": False,
        "retain_patient_characteristics": False,
        "date_processing": "offset",
        "retain_descriptors": False,
        "patient_pseudo_id_prefix": "<PREFIX ID> - "
    }   
    user_options['input_dcm_dp'] = default_options['input_dcm_dp']
    user_options['output_dcm_dp'] = default_options['output_dcm_dp']
    with open(file = './tmp/session-data/user-options.json', mode = 'w') as file:
        json.dump(user_options, file)
    session, dicom_pair_fps = dicom_deidentifier(SESSION_FP = './tmp/session-data/session.json')
    with open(file = './tmp/session-data/session.json', mode = 'w') as file:
        json.dump(session, file)
    prepare_medsam()
    initialize_masks()
    return dicom_pair_fps

def initialize_masks():
    global seg_masks
    seg_masks = []
    global classes
    classes = []
    for inpIdx in range(len(embeddings)):
        seg_masks.append(np.zeros(shape = (Hs[idx], Ws[idx]), dtype = np.uint8) for idx in range(len(embeddings)))

class MedSAM_Lite(nn.Module):
    def __init__(
            self, 
            image_encoder, 
            mask_decoder,
            prompt_encoder
        ):
        super().__init__()
        self.image_encoder = image_encoder
        self.mask_decoder = mask_decoder
        self.prompt_encoder = prompt_encoder

    def forward(self, image, box_np):
        image_embedding = self.image_encoder(image)
        with torch.no_grad():
            box_torch = torch.as_tensor(box_np, dtype=torch.float32, device='cpu')
            if len(box_torch.shape) == 2:
                box_torch = box_torch[:, None, :]
        sparse_embeddings, dense_embeddings = self.prompt_encoder(
            points=None,
            boxes=box_np,
            masks=None,
        )
        low_res_masks, iou_predictions = self.mask_decoder(
            image_embeddings=image_embedding,
            image_pe=self.prompt_encoder.get_dense_pe(),
            sparse_prompt_embeddings=sparse_embeddings,
            dense_prompt_embeddings=dense_embeddings,
            multimask_output=False,
          )
        return low_res_masks

    @torch.no_grad()
    def postprocess_masks(self, masks, new_size, original_size):
        masks = masks[..., :new_size[0], :new_size[1]]
        masks = F.interpolate(
            masks,
            size=(original_size[0], original_size[1]),
            mode="bilinear",
            align_corners=False,
        )
        return masks

def prepare_medsam():
    global embeddings
    embeddings = []
    global Hs, Ws
    Hs, Ws = [], []
    global medsam_model
    global newh, neww
    medsam_lite_image_encoder = TinyViT(
        img_size=256,
        in_chans=3,
        embed_dims=[
            64,
            128,
            160,
            320
        ],
        depths=[2, 2, 6, 2],
        num_heads=[2, 4, 5, 10],
        window_sizes=[7, 7, 14, 7],
        mlp_ratio=4.,
        drop_rate=0.,
        drop_path_rate=0.0,
        use_checkpoint=False,
        mbconv_expand_ratio=4.0,
        local_conv_size=3,
        layer_lr_decay=0.8
    )
    medsam_lite_prompt_encoder = PromptEncoder(
        embed_dim=256,
        image_embedding_size=(64, 64),
        input_image_size=(256, 256),
        mask_in_chans=16
    )
    medsam_lite_mask_decoder = MaskDecoder(
        num_multimask_outputs=3,
            transformer=TwoWayTransformer(
                depth=2,
                embedding_dim=256,
                mlp_dim=2048,
                num_heads=8,
            ),
            transformer_dim=256,
            iou_head_depth=3,
            iou_head_hidden_dim=256,
    )
    medsam_model = MedSAM_Lite(
        image_encoder = medsam_lite_image_encoder,
        mask_decoder = medsam_lite_mask_decoder,
        prompt_encoder = medsam_lite_prompt_encoder
    )
    medsam_lite_checkpoint = torch.load('./prm/lite_medsam.pth', map_location='cpu')
    medsam_model.load_state_dict(medsam_lite_checkpoint)
    medsam_model.to('cpu')
    print('MedSAM model deserialization completed')
    dcm_fps = sorted(glob('./tmp/session-data/raw/*'))
    t0 = time.time()
    print('Initializing MedSAM embeddings')
    for dcm_fp in dcm_fps:
        img = pydicom.dcmread(dcm_fp).pixel_array
        if len(img.shape) == 2:
            img_3c = np.repeat(img[:, :, None], 3, axis=-1)
        else:
            img_3c = img
        H, W, _ = img_3c.shape
        Hs.append(H)
        Ws.append(W)
        img_256 = cv2.resize(src = img_3c, dsize = (256, 256)).astype(np.float32)
        newh, neww = img_256.shape[:2]
        img_256 = (img_256 - img_256.min()) / np.clip(
            img_256.max() - img_256.min(), a_min=1e-8, a_max=None
        ) 
        img_256_tensor = (
            torch.tensor(img_256).float().permute(2, 0, 1).unsqueeze(0)
        )
        with torch.no_grad():
            embeddings.append(medsam_model.image_encoder(img_256_tensor))
    print('Initialization completed - %.2f'%(time.time()-t0))
def dicom_deidentifier(SESSION_FP: Union[None, str] = None) -> tuple[dict, list[tuple[str]]]:
    GPU = True
    if (not GPU):
        tf.config.set_visible_devices([], 'GPU')
        print('[DISABLED] PARALLEL COMPUTATION\n\n---')
    elif len(tf.config.list_physical_devices('GPU')) == 0:
        print('W: No GPU detected, switching to CPU instead')
        print('[DISABLED] PARALLEL COMPUTATION\n\n---')
    elif tf.config.list_physical_devices('GPU')[0][1] == 'GPU':
        print('[ENABLED] PARALLEL COMPUTATION\n\n---')
    if os.path.isfile('./tmp/session-data/custom-config.csv'):
        custom_config_df = pd.read_csv(filepath_or_buffer = './tmp/session-data/custom-config.csv', index_col = 0)
        custom_config_df.index = custom_config_df.index.str.strip("'")
    else:
        custom_config_df = None
    action_groups_df = pd.read_csv(filepath_or_buffer = './python/tmp/action-groups-dcm.csv', index_col = 0)
    if SESSION_FP == None or not os.path.isfile(SESSION_FP):
        print('Creating a new session')
        session = dict()
    else:
        with open(file = './tmp/session-data/session.json', mode = 'r') as file:
            print('Parsing already generated session')
            session = json.load(file)
    if os.path.isfile('./tmp/session-data/user-options.json'):
        with open(file = './tmp/session-data/user-options.json', mode = 'r') as file:
            user_input = json.load(file)
    else:
        exit('E: No client de-identification configuration was provided')
    pseudo_patient_ids = []
    for patient_deidentification_properties in session.values():
        pseudo_patient_ids.append(int(patient_deidentification_properties['patientPseudoId']))
    if pseudo_patient_ids == []:
        max_pseudo_patient_id = -1
    else:
        max_pseudo_patient_id = max(pseudo_patient_ids)
    requested_action_group_df = get_action_group(user_input = user_input, action_groups_df = action_groups_df, custom_config_df = custom_config_df)
    requested_action_group_df.to_csv('./tmp/session-data/requested-action-group-dcm.csv')
    rw_obj = rwdcm(in_dp = user_input['input_dcm_dp'], out_dp = user_input['output_dcm_dp'])
    while next(rw_obj):
        dcm = rw_obj.parse_file()
        if dcm == False:
            print('File already converted\nSkipping\n---\n')
            continue
        print('Processing DICOM file')
        date_processing_choices = {'keep', 'offset', 'remove'}
        assert user_input['date_processing'] in date_processing_choices, 'E: Invalid date processing input'
        real_patient_id = dcm[0x0010, 0x0020].value
        patient_deidentification_properties = session.get(real_patient_id, False)
        if not patient_deidentification_properties:
            max_pseudo_patient_id += 1
            session[real_patient_id] = {'patientPseudoId': '%.6d'%max_pseudo_patient_id}
            days_total_offset = round(random.uniform(10 * 365, (2 * 10) * 365))
            seconds_total_offset = round(random.uniform(0, 24 * 60 * 60))
        else:
            days_total_offset = session[real_patient_id]['daysOffset']
            seconds_total_offset = session[real_patient_id]['secondsOffset']
        dcm, tag_value_replacements = adjust_dicom_metadata\
        (
            dcm = dcm,
            action_group_fp = './tmp/session-data/requested-action-group-dcm.csv',
            patient_pseudo_id = session[real_patient_id]['patientPseudoId'],
            days_total_offset = days_total_offset,
            seconds_total_offset = seconds_total_offset
        )
        session[real_patient_id]['daysOffset'] = tag_value_replacements['days_total_offset']
        session[real_patient_id]['secondsOffset'] = tag_value_replacements['seconds_total_offset']
        dcm = deidentification_attributes(user_input = user_input, dcm = dcm)
        if user_input['clean_image']:
            dcm = image_deintentifier(dcm = dcm)
        print('DICOM Processing Completed')
        rw_obj.export_processed_file(dcm = dcm)
        rw_obj.export_session(session = session)
    print('\nOperation completed')
    return session, rw_obj.dicom_pair_fps

@torch.no_grad()
def medsam_inference(medsam_model, img_embed, box_256, new_size, original_size):
    box_torch = torch.as_tensor(box_256, dtype=torch.float, device=img_embed.device)
    if len(box_torch.shape) == 2:
        box_torch = box_torch[:, None, :]
    sparse_embeddings, dense_embeddings = medsam_model.prompt_encoder(
        points = None,
        boxes = box_torch,
        masks = None,
    )
    low_res_logits, _ = medsam_model.mask_decoder(
        image_embeddings=img_embed,
        image_pe=medsam_model.prompt_encoder.get_dense_pe(),
        sparse_prompt_embeddings=sparse_embeddings,
        dense_prompt_embeddings=dense_embeddings,
        multimask_output=False
    )
    low_res_pred = medsam_model.postprocess_masks(low_res_logits, new_size, original_size)
    low_res_pred = torch.sigmoid(low_res_pred)
    low_res_pred = low_res_pred.squeeze().cpu().numpy()
    medsam_seg = (low_res_pred > 0.5).astype(np.uint8)
    return medsam_seg

def attach_segm_data(dcm: pydicom.dataset.FileDataset, seg_mask: np.array, class_names: list[str]) -> pydicom.dataset.FileDataset:
    assert type(seg_mask[0, 0]) == np.uint8, 'E: Incompatible element-wise data type'
    seg_dataset = pydicom.dataset.Dataset()
    img = dcm.pixel_array
    assert len(img.shape) == 2, 'E: Incompatible image shape'
    seg_dataset.Rows, seg_dataset.Columns = img.shape
    seg_dataset.SOPClassUID = '1.2.840.10008.5.1.4.1.1.66.4'
    seg_dataset.BitsAllocated = 8
    seg_dataset.SegmentDescription = ';'.join(class_names)
    seg_dataset.PixelData = seg_mask.tobytes()
    dcm.SegmentSequence = pydicom.sequence.Sequence([seg_dataset])
    return dcm

def deidentification_attributes(user_input: dict, dcm: pydicom.dataset.FileDataset) -> pydicom.dataset.FileDataset:
    user_input_lookup_table = \
    {
        'clean_image': '113101',
        'retain_safe_private': '113111',
        'retain_uids': '113110',
        'retain_device_identity': '113109',
        'retain_patient_characteristics': '113108',
        'date_processing':
        {
            'offset': '113107',
            'keep': '113106',
        },
        'retain_descriptors': '113105',
    }
    DeIdentificationCodeSequence = 'DCM:11310'
    assert set(user_input_lookup_table.keys()).issubset(set(user_input.keys())), 'E: Inconsistency with user input keys with lookup de-identification table keys'
    for OptionName, DeIdentificationCodes in user_input_lookup_table.items():
        choice = user_input[OptionName]
        if OptionName == 'date_processing':
            if choice in user_input_lookup_table['date_processing']:
                DeIdentificationCodeSequence += '/' + user_input_lookup_table['date_processing'][choice]
        else:
            if choice:
                DeIdentificationCodeSequence += '/' + user_input_lookup_table[OptionName]
    dcm.add_new\
    (
        tag = (0x0012, 0x0062),
        VR = 'LO',
        value = 'YES'
    )
    dcm.add_new\
    (
        tag = (0x0012, 0x0063),
        VR = 'LO',
        value = DeIdentificationCodeSequence
    )
    if user_input['clean_image']:
        dcm.add_new\
        (
            tag = (0x0028, 0x0301),
            VR = 'LO',
            value = 'NO'
        )
    return dcm

def ndarray_size(arr: np.ndarray) -> int:
    return arr.itemsize*arr.size

def image_preprocessing(img: np.ndarray, downscale_dimensionality: tuple[int], multichannel: bool = True, retain_aspect_ratio: bool = False) -> np.ndarray:
    img = (255.0 * ((img - np.min(img)) / (np.max(img) - np.min(img)))).astype(np.uint8)
    if downscale_dimensionality:
        if retain_aspect_ratio:
            aspr = img.shape[1]/img.shape[0]
            H = min([downscale_dimensionality, img.shape[0], img.shape[1]])
            W = int(H * aspr)
            new_shape = (H, W)
        else:
            new_shape = (min([downscale_dimensionality, img.shape[0]]), min([downscale_dimensionality, img.shape[1]]))
        img = cv2.resize(img, (new_shape[1], new_shape[0]))
    if (multichannel) and (len(img.shape) == 2):
        img = np.stack(3*[img], axis = -1)
    return img

def bbox_area_distorter(img: np.ndarray, bboxes: np.ndarray, initial_array_shape: tuple[int], downscaled_array_shape: tuple[int]) -> np.ndarray:
    reducted_region_color = np.mean(img).astype(np.uint16)
    multiplicative_mask = np.ones(downscaled_array_shape, dtype = np.uint8)
    additive_mask = np.zeros(initial_array_shape, dtype = np.uint8)
    for bbox in bboxes:
        x0, y0 = bbox[0, 0:(1+1)]
        x1, y1 = bbox[1, 0:(1+1)]
        x2, y2 = bbox[2, 0:(1+1)]
        x3, y3 = bbox[3, 0:(1+1)]
        rectangle = np.array\
        (
            [
                [
                    [x0, y0],
                    [x1, y1],
                    [x2, y2],
                    [x3, y3]
                ]
            ],
            dtype = np.int32 
        )
        cv2.fillPoly(multiplicative_mask, rectangle, 0)
    multiplicative_mask = cv2.resize(multiplicative_mask, (initial_array_shape[1], initial_array_shape[0]), interpolation = cv2.INTER_NEAREST)
    additive_mask = reducted_region_color * (multiplicative_mask == 0)
    img_ = img.copy()
    img_ = (img_ * multiplicative_mask + additive_mask)
    return img_

def image_deintentifier(dcm: pydicom.dataset.FileDataset) -> pydicom.dataset.FileDataset:
    min_dim = 50
    downscale_dimensionality = 1024
    assert downscale_dimensionality >= min_dim, 'E: Downscale dimensionality is excessively small'
    dcm.decompress()
    raw_img_uint16_grayscale = dcm.pixel_array
    if min(raw_img_uint16_grayscale.shape) < min_dim:
        print('W: Pixel data will not be affected because the DICOM image resolution is excessively small')
        return dcm
    print('Input DICOM file information')
    print('Input image shape: ', raw_img_uint16_grayscale.shape)
    if downscale_dimensionality < max(raw_img_uint16_grayscale.shape[0], raw_img_uint16_grayscale.shape[1]):
        print('Downscaling detection input image from shape (%d, %d) to (%d, %d)'%(raw_img_uint16_grayscale.shape[0], raw_img_uint16_grayscale.shape[1], downscale_dimensionality, downscale_dimensionality))

    raw_img_uint8_rgb = image_preprocessing(img = raw_img_uint16_grayscale, downscale_dimensionality = downscale_dimensionality)

    pipeline = keras_ocr.detection.Detector()
    bboxes = pipeline.detect([raw_img_uint8_rgb])[0]
    initial_array_shape = raw_img_uint16_grayscale.shape
    downscaled_array_shape = raw_img_uint8_rgb.shape[:-1]
    if np.size(bboxes) != 0:
        cleaned_img = bbox_area_distorter\
        (
            img = raw_img_uint16_grayscale,
            bboxes = bboxes,
            initial_array_shape = initial_array_shape,
            downscaled_array_shape = downscaled_array_shape
        )
        dcm.PixelData = cleaned_img.tobytes()
    else:
        print('Image state: No text detected')
    return dcm
def get_action_group(user_input: dict, action_groups_df: pd.core.frame.DataFrame, custom_config_df: Union[pd.core.frame.DataFrame, None]) -> pd.core.frame.DataFrame:
    def merge_action(primary_srs: pd.core.series.Series, Action2BeAssigned_srs: pd.core.series.Series) -> pd.core.series.Series:
        return primary_srs.where\
        (
            cond = Action2BeAssigned_srs.isna(),
            other = Action2BeAssigned_srs,
            axis = 0,
            inplace = False,
        )
    def merge_with_custom_user_config_file(requested_action_group_df, custom_config_df):
        valid_actions = {'X', 'K', 'C'}
        if not set(custom_config_df['Action']).issubset(valid_actions):
            print('E: "Action" values in the Custom table must be either "X", "K", or "C". Please correct the data.')
            exit()
        requested_action_group_df = requested_action_group_df.merge(custom_config_df[['Action']], left_index=True, right_index=True, how='left')
        requested_action_group_df.loc[requested_action_group_df['Action'].isin(['X', 'K', 'C']), 'Requested Action Group'] = requested_action_group_df['Action']
        requested_action_group_df.drop(columns=['Action'], inplace=True)
        return requested_action_group_df
    requested_action_group_df = pd.DataFrame\
    (
        data = action_groups_df['Default'].to_list(),
        columns = ['Requested Action Group'],
        index = action_groups_df.index
    )
    requested_action_group_df.insert\
    (
        loc = 0,
        column = 'Name',
        value = action_groups_df['Name'].to_list(),
    )
    if user_input['retain_safe_private']:
        requested_action_group_df['Requested Action Group'] = merge_action(primary_srs = requested_action_group_df['Requested Action Group'], Action2BeAssigned_srs = action_groups_df['Rtn. Safe Priv. Opt.'])
    if user_input['retain_uids']:
        requested_action_group_df['Requested Action Group'] = merge_action(primary_srs = requested_action_group_df['Requested Action Group'], Action2BeAssigned_srs = action_groups_df['Rtn. UIDs Opt.'])
    if user_input['retain_device_identity']:
        requested_action_group_df['Requested Action Group'] = merge_action(primary_srs = requested_action_group_df['Requested Action Group'], Action2BeAssigned_srs = action_groups_df['Rtn. Dev. Id. Opt.'])
    if user_input['retain_patient_characteristics']:
        requested_action_group_df['Requested Action Group'] = merge_action(primary_srs = requested_action_group_df['Requested Action Group'], Action2BeAssigned_srs = action_groups_df['Rtn. Pat. Chars. Opt.'])
    if user_input['date_processing'] == 'keep':
        requested_action_group_df['Requested Action Group'] = merge_action(primary_srs = requested_action_group_df['Requested Action Group'], Action2BeAssigned_srs = action_groups_df['Rtn. Long. Modif. Dates Opt.'])
    elif user_input['date_processing'] == 'offset':
        requested_action_group_df['Requested Action Group'] = merge_action(primary_srs = requested_action_group_df['Requested Action Group'], Action2BeAssigned_srs = action_groups_df['Offset Long. Modif. Dates Opt.'])
    elif user_input['date_processing'] == 'remove':
        requested_action_group_df['Requested Action Group'] = merge_action(primary_srs = requested_action_group_df['Requested Action Group'], Action2BeAssigned_srs = action_groups_df['Remove Long. Modif. Dates Opt.'])
    if user_input['retain_descriptors']:
        requested_action_group_df['Requested Action Group'] = merge_action(primary_srs = requested_action_group_df['Requested Action Group'], Action2BeAssigned_srs = action_groups_df['Rtn. Desc. Opt.'])
    if type(custom_config_df) == pd.core.frame.DataFrame:
        requested_action_group_df = merge_with_custom_user_config_file(requested_action_group_df, custom_config_df)
    return requested_action_group_df

def adjust_dicom_metadata(dcm: pydicom.dataset.FileDataset, action_group_fp: str, patient_pseudo_id: str, days_total_offset: int, seconds_total_offset: int) -> tuple[pydicom.dataset.FileDataset, dict]:

    def add_date_offset(input_date_str: str, days_total_offset: str) -> str:
        input_date = datetime.datetime.strptime(input_date_str, '%Y%m%d')
        output_date = input_date + datetime.timedelta(days = days_total_offset)
        output_date_str = output_date.strftime('%Y%m%d')
        return output_date_str

    def seconds2daytime(seconds_total_offset: int) -> str:
        output_hours = seconds_total_offset // 3600
        output_minutes = (seconds_total_offset % 3600) // 60
        output_seconds = (seconds_total_offset % 3600) % 60
        output_time_str = '%.2d%.2d%.2d'%(output_hours, output_minutes, output_seconds)
        return output_time_str

    def recursive_SQ_cleaner(ds: pydicom.dataset.FileDataset, action: str, action_attr_tag_idx: str) -> pydicom.dataset.FileDataset:
        for ds_attr in ds:
            ds_tag_idx = re.sub('[(,) ]', '', str(ds_attr.tag))
            if ds[ds_tag_idx].VR == 'SQ':
                for inner_ds_idx in range(ds[ds_tag_idx].VM):
                    ds[ds_tag_idx].value[inner_ds_idx] = recursive_SQ_cleaner\
                    (
                        ds = ds[ds_tag_idx][inner_ds_idx],
                        action = action,
                        action_attr_tag_idx = action_attr_tag_idx
                    )
            elif action_attr_tag_idx == ds_tag_idx:
                if action == 'Z':
                    assert ds_tag_idx in ['00100010', '00100020'], 'E: Cannot apply action code `Z` in any other attribute besides Patient ID and Patient Name; the issue is likely on the action group config object'
                    ds[ds_tag_idx].value = patient_pseudo_id
                elif action == 'X':
                    ds[ds_tag_idx].value = ''
                elif action == 'C':
                    if ds[ds_tag_idx].value != '' and ds[ds_tag_idx].VR == 'DA':
                        tag_value_replacements['days_total_offset'] = days_total_offset
                        ds[ds_tag_idx].value = add_date_offset(input_date_str = ds[ds_tag_idx].value, days_total_offset = days_total_offset)
                    elif ds[ds_tag_idx].VR == 'TM':
                        tag_value_replacements['seconds_total_offset'] = seconds_total_offset
                        ds[ds_tag_idx].value = seconds2daytime(seconds_total_offset = tag_value_replacements['seconds_total_offset'])
        return ds

    action_group_df = pd.read_csv(filepath_or_buffer = action_group_fp, index_col = 0)
    tag_value_replacements = dict()
    tag_value_replacements['days_total_offset'] = 0
    tag_value_replacements['seconds_total_offset'] = 0
    for action_attr_tag_idx in action_group_df.index:
        action = action_group_df.loc[action_attr_tag_idx].iloc[1]
        dcm = recursive_SQ_cleaner\
        (
            ds = dcm,
            action = action,
            action_attr_tag_idx = action_attr_tag_idx
        )
    return dcm, tag_value_replacements
class rwdcm:
    def __init__(self, in_dp: str, out_dp: str):
        self.SAFETY_SWITCH = True
        if not self.SAFETY_SWITCH:
            print('W: Safety switch is off. Output directory can now be deleted.')
        if in_dp[-1] != '/': in_dp = in_dp + '/'
        self.raw_data_dp = in_dp
        self.raw_dicom_paths = sorted(self.get_dicom_paths(data_dp = self.raw_data_dp))
        self.dicom_pair_fps = []
        self.clean_data_dp = out_dp + '/' + 'de-identified-files/'
        already_cleaned_dicom_paths = self.get_dicom_paths(data_dp = self.clean_data_dp)
        self.hashes_of_already_converted_files = [already_cleaned_dicom_path.split('/')[-1].split('.')[0] for already_cleaned_dicom_path in already_cleaned_dicom_paths]
        self.n_dicom_files = len(self.raw_dicom_paths)
        print('\nTotal number of DICOM files existing inside the input directory:\n%d'%(self.n_dicom_files))
        print('---', end = 2 * '\n')
        self.DICOM_IDX = -1

    def __next__(self) -> bool:
        self.DICOM_IDX += 1
        if self.DICOM_IDX <= self.n_dicom_files - 1:
            self.raw_dicom_path = self.raw_dicom_paths[self.DICOM_IDX]
            print('---\n')
            print('DICOM List Index:', self.DICOM_IDX)
            return True
        else:
            return False

    def get_dicom_paths(self, data_dp: str) -> list:
        dicom_paths = \
        (
            glob\
            (
                pathname = data_dp + '*',
                recursive = True
            )
        )
        proper_dicom_paths = []
        for dicom_path in dicom_paths:
            try:
                pydicom.dcmread(dicom_path)
                proper_dicom_paths.append(dicom_path)
            except InvalidDicomError:
                continue
        return proper_dicom_paths

    def parse_file(self) -> Union[pydicom.dataset.FileDataset, bool]:
        self.input_dicom_hash = hashlib.sha256(self.raw_dicom_path.encode('UTF-8')).hexdigest()
        if self.input_dicom_hash in self.hashes_of_already_converted_files:
            return False
        else:
            dcm = pydicom.dcmread(self.raw_dicom_path)
            print('Parsed\n%s'%(self.raw_dicom_path))
            return dcm

    def export_processed_file(self, dcm: pydicom.dataset.FileDataset):
        self.clean_dicom_dp = self.clean_data_dp + str(dcm[0x0010, 0x0020].value) + '/' + str(dcm[0x0008, 0x0060].value) + '/' + str(dcm[0x0020, 0x0011].value)
        if not os.path.exists(self.clean_dicom_dp):
            os.makedirs(self.clean_dicom_dp)
        clean_dicom_fp = self.clean_dicom_dp + '/' + self.input_dicom_hash + '.dcm'
        print('Exporting file at\n%s'%(clean_dicom_fp))
        dcm.save_as(clean_dicom_fp)
        self.dicom_pair_fps.append((self.raw_dicom_path, clean_dicom_fp))

    def export_session(self, session: dict):
        with open(self.clean_data_dp + '/session.json', 'w') as file:
            json.dump(session, file)

if __name__ == "__main__":
    if(os.getenv("STAGING")):
        os.makedirs('tmp/session-data/raw', exist_ok=True)
        os.makedirs('tmp/session-data/clean', exist_ok=True)
        run(app, host="0.0.0.0", port=8000)