#!usr/bin/python
from __future__ import absolute_import

import numpy as np
import xml.etree.ElementTree as ET
from PIL import Image
from PIL import ImageEnhance
import cv2
import glob

import argparse
import os
import sys


import pickle

import torch
import torch.nn as nn
from torch.backends import cudnn
from torch.utils.data import DataLoader, SubsetRandomSampler

sys.path.append(os.path.dirname(os.path.realpath(__file__)))

from file_list_final import file_train_list, file_val_list

from lib.datasets.dataset import AlignCollate, ResizeNormalize
from lib.models.resnet_aster import ResNet_ASTER
from lib.models.attention_recognition_head import AttentionRecognitionHead




current_path = os.path.dirname(os.path.abspath(__file__)) 

def get_data(filename, args):
    """
    @ input
    filename: ex. kr00001973962b1p-4
    
    @ output
    image, coordinates, labels
    """
    
    xmlfile = filename
    jpgfile = filename.replace(".xml",".jpg")

    doc = ET.parse(xmlfile)
    root = doc.getroot()
    object_dict = {}
    for x in root.findall('object'):
        coord = '/'.join([x.find('bndbox').find('xmin').text,
                x.find('bndbox').find('ymin').text,
                x.find('bndbox').find('xmax').text,
                x.find('bndbox').find('ymax').text])
        label = x.find('name').text
        object_dict[coord] = label

    if args.image_format == 'cv2':
        image = cv2.imread(jpgfile, cv2.IMREAD_COLOR)
    else:
        image = Image.open(jpgfile)

    coordinates = []
    labels = []
    for key in object_dict.keys():
        coord = tuple([int(x) for x in key.split('/')])
        coordinates.append(coord)
        labels.append(object_dict[key])
        
    return image, coordinates, labels

def crop_image(image,coordinates, args, resample = Image.BICUBIC):
    """
    @Input
    image: an image (PIL)
    coordinates: a list of coordinates of text which should be cropped
    
    @Output
    cropped_images: a list of cropped images
    """

    #   PIL coordinate : (w0, h0, w1, h1)
    #   cv2 coordinate : [h0:h1, w0:w1]

    cropped_images = []
    for i,coordinate in enumerate(coordinates):
        if args.image_format == 'cv2':
            cropped_image = image[coordinate[1]:coordinate[3], coordinate[0]:coordinate[2]]
            
        else:
            cropped_image = image.crop(coordinate)

            # resize
            h, w = cropped_image.size

            # ratio = 1.5
            cropped_image = cropped_image.resize((int(h*args.resize), int(w*args.resize)),
                                                    resample= resample)
            sharpness_enhancer = ImageEnhance.Sharpness(cropped_image)
            cropped_image = sharpness_enhancer.enhance(args.sharpness)
            contrast_enhancer = ImageEnhance.Contrast(cropped_image)
            cropped_image = contrast_enhancer.enhance(args.contrast)
            
        cropped_images.append(cropped_image)
    
    return cropped_images

def Create_char_dict():
    """
    Returns charactors 2 indexes / indexes 2 charactors dictionaries.
    """

    import string
    voc = list(string.printable[:-6])
    voc.append('EOS')
    voc.append('PADDING')
    voc.append('UNKNOWN')

    char2id_dict = dict(zip(voc, range(len(voc))))
    id2char_dict = dict(zip(range(len(voc)), voc))

    return char2id_dict, id2char_dict



def get_data_image(filename, args):
    """
    @ input
    filename: ex. kr00001973962b1p-4
    
    @ output
    image
    """
    
    jpgfile = filename.replace(".xml",".jpg")
    
    if args.image_format == 'cv2':
        image = cv2.imread(jpgfile, cv2.IMREAD_COLOR)
    else:
        image = Image.open(jpgfile)

    return image


class Pred_Aster():
    def __init__(self, cuda):
        """
        cuda : set to True for GPU usage, False for CPU usage
        """

        from pred_params import Get_ocr_args
        args = Get_ocr_args()

        args.cuda = cuda
         
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        if args.cuda == True:
            torch.cuda.manual_seed(args.seed)
            torch.cuda.manual_seed_all(args.seed)
            cudnn.benchmark = True
            torch.backends.cudnn.deterministic = True

        if args.cuda:
            print('using cuda.')
            torch.set_default_tensor_type('torch.cuda.FloatTensor')
        else:
            print('using cpu.')
            torch.set_default_tensor_type('torch.FloatTensor')

        #   Create Character dict & max seq len
        char2id_dict , id2char_dict= Create_char_dict()

        print(id2char_dict)
        rec_num_classes = len(id2char_dict)

        #   Get rec num classes / max len
        print('max len : '+str(args.max_len))


        #   init model

        encoder = ResNet_ASTER(with_lstm = True, n_group = args.n_group, use_cuda = args.cuda)

        encoder_out_planes = encoder.out_planes

        decoder = AttentionRecognitionHead(num_classes = rec_num_classes,
                                            in_planes = encoder_out_planes,
                                            sDim = args.decoder_sdim,
                                            attDim = args.attDim,
                                            max_len_labels = args.max_len,
                                            use_cuda = args.cuda)

        #   this is where you acquire trained parameters
        if args.cuda == True:
            encoder.load_state_dict(torch.load(current_path + '/../params/encoder_final'))
            decoder.load_state_dict(torch.load(current_path + '/../params/decoder_final'))
        else:
            encoder.load_state_dict(torch.load(current_path + '/../params/encoder_final',map_location=torch.device('cpu')))
            decoder.load_state_dict(torch.load(current_path + '/../params/decoder_final',map_location=torch.device('cpu')))
        print('fine-tuned model loaded')


        if args.cuda == True:
            device = torch.device('cuda')
            encoder.to(device)
            decoder.to(device)
            self.device = device
        else:
            pass

        self.encoder = encoder
        self.decoder = decoder
        
        self.args = args
        self.char2id_dict = char2id_dict
        self.id2char_dict = id2char_dict

    def idx2char(self, pred, id2char_dict, eos='EOS'):
        eos_idx = [x for x in id2char_dict.keys() if id2char_dict[x]==eos][0]
        pred = pred[:pred.tolist().index(eos_idx)]
        pred = [id2char_dict[x] for x in pred]
        pred_char = ''.join(pred)
        return pred_char


    def forward(self, image_path, coordinates):

        """
        @input
        image paths : One image path without '.xml' or '.png'
        coordinates: A List of coordinates

        @output : A List of characters
        """

        args = self.args
        encoder = self.encoder
        decoder = self.decoder
        if args.cuda:
            device = self.device

        image = get_data_image(image_path, args)

        cropped_images = crop_image(image,coordinates, args, resample = Image.BICUBIC) # list of imgs

        cropped_images = [{'images' : x, 'rec_targets' : 0, 'rec_lengths' : 0} 
                           for x in cropped_images]

        #   data loader
        test_pred = []
        test_image = []

        test_loader = DataLoader(cropped_images, 
                                batch_size = args.batch_size,
                                shuffle = False,
                                collate_fn = AlignCollate(
                                    imgH = args.height, imgW = args.width, keep_ratio = True)
                                )

        for batch_idx, batch in enumerate(test_loader):
            if args.cuda:
                x = batch[0].to(device)
            else:
                x = batch[0]

            encoder_feats = self.encoder(x)
            rec_pred, rec_pred_scores = decoder.beam_search(encoder_feats,\
                                                    args.beam_width, args.eos)

            rec_pred = rec_pred.detach().cpu().numpy()
            test_pred.extend(rec_pred)
            test_image.extend(x.detach().cpu().numpy())

        test_pred_char = [self.idx2char(x, self.id2char_dict) for x in test_pred]

        return test_pred_char



if __name__ == "__main__":

    filenames = [x+'.xml' for x in file_val_list]

    from pred_params import Get_ocr_args
    args = Get_ocr_args()

    use_cuda=False

    model = Pred_Aster(use_cuda)

    for i, filename in enumerate(filenames):
        
        if i < 10:
            image, coordinates, labels = get_data(filename, args)

            pred_char = model.forward(filename, coordinates)

            print(pred_char)
            print(labels)

