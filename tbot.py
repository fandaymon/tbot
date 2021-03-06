import logging
import asyncio

from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.markdown import text, hbold, italic, code, pre
from aiogram.types import ReplyKeyboardRemove, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton


import torch 
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import transforms , models 
from PIL import Image 
import matplotlib.pyplot as plt
import numpy as np
import threading
import os
import copy

def gram_matrix(input):
    batch_size , h, w, f_map_num = input.size()  # batch size(=1)
    # b=number of feature maps
    # (h,w)=dimensions of a feature map (N=h*w)

    features = input.view(batch_size * h, w * f_map_num)  

    G = torch.mm(features, features.t())  # compute the gram product

    # we 'normalize' the values of the gram matrix
    # by dividing by the number of element in each feature maps.
    return G.div(batch_size * h * w * f_map_num)

class ContentLoss(nn.Module):

        def __init__(self, target,):
            super(ContentLoss, self).__init__()
            # we 'detach' the target content from the tree used
            # to dynamically compute the gradient: this is a stated value,
            # not a variable. Otherwise the forward method of the criterion
            # will throw an error.
            self.target = target.detach()#это константа. Убираем ее из дерева вычеслений
            self.loss = F.mse_loss(self.target, self.target )#to initialize with something

        def forward(self, input):
            self.loss = F.mse_loss(input, self.target)
            return input

class StyleLoss(nn.Module):
        def __init__(self, target_feature, sl_weight):
            super(StyleLoss, self).__init__()
            self.sl_weight = sl_weight
            self.target = gram_matrix(target_feature).detach()
            self.loss = self.sl_weight * F.mse_loss(self.target, self.target)# to initialize with something


        def forward(self, input):
            G = gram_matrix(input)
            self.loss = self.sl_weight * F.mse_loss(G, self.target)
            return input

class Normalization(nn.Module):
        def __init__(self, mean, std):
            super(Normalization, self).__init__()
            # .view the mean and std to make them [C x 1 x 1] so that they can
            # directly work with image Tensor of shape [B x C x H x W].
            # B is batch size. C is number of channels. H is height and W is width.
            self.mean = torch.tensor(mean).view(-1, 1, 1)
            self.std = torch.tensor(std).view(-1, 1, 1)

        def forward(self, img):
            # normalize img
            return (img - self.mean) / self.std

class Style_transfer:
    def __init__(self):
        self.device = 'cuda'
        self.cnn_normalization_mean = torch.tensor([0.485, 0.456, 0.406]).to(self.device)
        self.cnn_normalization_std = torch.tensor([0.229, 0.224, 0.225]).to(self.device)
        self.content_layers = ['conv_4']
#        self.style_layers = ['conv_1', 'conv_2', 'conv_3', 'conv_4', 'conv_5']

        self.style_layers = {"conv_1" : 1.0, 
                         "conv_2" : 0.7,
                         "conv_3" : 0.2,
                         "conv_4" : 0.2,
                         "conv_5" : 0.2}

        self.cnn = models.vgg19(pretrained=True).features.to(self.device).eval()

        self.busy = 0 
        self.last_sl = 100


    def get_style_model_and_losses(self, cnn, normalization_mean, normalization_std,
                                       style_img, content_img):
            cnn = copy.deepcopy(cnn)

            # normalization module
            normalization = Normalization(normalization_mean, normalization_std).to(self.device)

            # just in order to have an iterable access to or list of content/syle
            # losses
            content_losses = []
            style_losses = []

            # assuming that cnn is a nn.Sequential, so we make a new nn.Sequential
            # to put in modules that are supposed to be activated sequentially
            model = nn.Sequential(normalization)

            i = 0  # increment every time we see a conv
            for layer in cnn.children():
                if isinstance(layer, nn.Conv2d):
                    i += 1
                    name = 'conv_{}'.format(i)
                elif isinstance(layer, nn.ReLU):
                    name = 'relu_{}'.format(i)
                    # The in-place version doesn't play very nicely with the ContentLoss
                    # and StyleLoss we insert below. So we replace with out-of-place
                    # ones here.
                    #Переопределим relu уровень
                    layer = nn.ReLU(inplace=False)
                elif isinstance(layer, nn.MaxPool2d):
                    name = 'pool_{}'.format(i)
                elif isinstance(layer, nn.BatchNorm2d):
                    name = 'bn_{}'.format(i)
                else:
                    raise RuntimeError('Unrecognized layer: {}'.format(layer.__class__.__name__))

                model.add_module(name, layer)

                if name in self.content_layers:
                    # add content loss:
                    target = model(content_img).detach()
                    content_loss = ContentLoss(target)
                    model.add_module("content_loss_{}".format(i), content_loss)
                    content_losses.append(content_loss)

                if name in self.style_layers:
                    # add style loss:
                    target_feature = model(style_img).detach()
                    style_loss =  StyleLoss(target_feature, self.style_layers[name]) 
                    model.add_module("style_loss_{}".format(i), style_loss)
                    style_losses.append(style_loss)

            # now we trim off the layers after the last content and style losses
            #выбрасываем все уровни после последенего styel loss или content loss
            for i in range(len(model) - 1, -1, -1):
                if isinstance(model[i], ContentLoss) or isinstance(model[i], StyleLoss):
                    break

            model = model[:(i + 1)]

            return model, style_losses, content_losses

    def get_input_optimizer(self, input_img):
            # this line to show that input is a parameter that requires a gradient
            #добоваляет содержимое тензора катринки в список изменяемых оптимизатором параметров
            optimizer = optim.LBFGS([input_img.requires_grad_()]) 
            return optimizer

    def imcnvt(self, image):
        im = image.to("cpu").clone().detach()
        im = im.numpy().squeeze()
        im = im.transpose(1,2,0)

        im = im.clip(0, 1)
        return im



    def style_transfer_train(self, content_file, style_file, user_id):
        self.busy = 1
        
        imsize = 512  
        num_steps = 400
        style_weight = 100000
        content_weight = 1

        loader = transforms.Compose([
            transforms.Resize(imsize),  # нормируем размер изображения
            transforms.CenterCrop(imsize),
            transforms.ToTensor()])  # превращаем в удобный формат
        

        content_img = Image.open(content_file)
        content_img = loader(content_img).unsqueeze(0)
        content_img =  content_img.to(self.device, torch.float)
    
        style_img = Image.open(style_file)
        style_img = loader(style_img).unsqueeze(0)
        style_img =  style_img.to(self.device, torch.float)

        input_img = content_img.clone()

        model, style_losses, content_losses = self.get_style_model_and_losses(self.cnn,
            self.cnn_normalization_mean, self.cnn_normalization_std, style_img, content_img)
        optimizer = self.get_input_optimizer(input_img)

        run = [0]
        min_sl = 100
        

        while run[0] <= num_steps:

            def closure():
                # correct the values 
                # это для того, чтобы значения тензора картинки не выходили за пределы [0;1]
                input_img.data.clamp_(0, 1)

                optimizer.zero_grad()

                model(input_img)

                style_score = 0
                content_score = 0

                for sl in style_losses:
                    style_score += sl.loss
                for cl in content_losses:
                    content_score += cl.loss
                
                #взвешивание ощибки
                style_score *= style_weight
                content_score *= content_weight

                loss = style_score + content_score
                loss.backward()

                run[0] += 1
                self.last_sl = style_score.item()
                if run[0] % 100 == 0:
                    print("run {}:".format(run))
                    print('Style Loss : {:4f} Content Loss: {:4f}'.format(
                        style_score.item(), content_score.item()))
                    print()

                return style_score + content_score

            optimizer.step(closure)
            if self.last_sl<min_sl:
                min_sl = self.last_sl
                min_img = self.imcnvt(input_img)


        input_img = min_img
        #input_img = self.imcnvt(input_img)
        plt.imsave('target' + user_id + '.png', input_img, format='png')

        self.busy = 0
     
        return 
    

API_TOKEN = ''


# Configure logging
logging.basicConfig(level=logging.INFO)

# Initialize bot and dispatcher
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
st = Style_transfer()

button_st = KeyboardButton('Style Transfer')
st_kb = ReplyKeyboardMarkup(resize_keyboard=True).add(button_st)




@dp.message_handler(commands=['st'])
async def process_file_command(message: types.Message):
    user_id = str(message.from_user.id)
    if os.path.exists('content' + user_id + '.jpg') == False:
        await bot.send_message(user_id, 'Отсутствует фотография контента')
        return
    
    if os.path.exists('style' + user_id + '.jpg') == False:
        await bot.send_message(user_id, 'Отсутствует фотография стиля')
        return

    await bot.send_message(user_id,'Перенос стиля запущен, через некоторое время вы получите результат')
    while st.busy == 1:
        await asyncio.sleep(2)
    x = threading.Thread(target=st.style_transfer_train, args=('content' + user_id + '.jpg','style' + user_id + '.jpg', user_id))
    x.start()  # делаем style transfer
    await st_transfer(user_id)
    


                            
                            
@dp.message_handler(commands=['start'])
async def process_start_command(message: types.Message):
    await message.reply('Бот для переноса стиля готов к работе!\nИспользуй /help, '
                        'чтобы узнать как со мной общаться!')


@dp.message_handler(commands=['help'])
async def process_help_command(message: types.Message):
    msg = text('Чтобы всё произошло, отправьте боту картинку со стилем с подписью style',
               'отправьте боту картинку с контентом с подписью content',
               'дайте боту команду /st и через некоторое время вы получите результат', sep='\n')
    await message.reply(msg, parse_mode=types.ParseMode.HTML)

@dp.message_handler()
async def echo(message: types.Message):
#    # old style:
#    # await bot.send_message(message.chat.id, message.text)
#
#    await message.answer(message.text)
    if message.text=='Style Transfer':
        user_id = str(message.from_user.id)
        await bot.send_message(user_id,'Перенос стиля запущен, через некоторое время вы получите результат')
        while st.busy == 1:
            await asyncio.sleep(2)
        x = threading.Thread(target=st.style_transfer_train, args=('content' + user_id + '.jpg','style' + user_id + '.jpg', user_id))
        x.start()  # делаем style transfer
        await st_transfer(user_id)


@dp.message_handler(content_types=['photo'])
async def handle_docs_photo(message):
    print(message.caption)
    await message.photo[-1].download(message.caption + str(message.from_user.id) + '.jpg')
    user_id = str(message.from_user.id)
    if os.path.exists('content' + user_id + '.jpg') and os.path.exists('style' + user_id + '.jpg') :
        await message.reply("Стиль и контент получены", reply_markup=st_kb)



async def st_transfer(user_id):

    while st.busy == 1:
        await asyncio.sleep(2)

    with open('target' + user_id + '.png', 'rb') as photo:
        await bot.send_photo(user_id, photo,
                        caption='Получите и распишитесь!')



if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
