import torch
import torch.nn as nn
import torch.nn.functional as F

import ipdb

from .custom_layers import EqualizedConv2d, EqualizedLinear,\
    NormalizationLayer, Upscale2d, AudioNorm, \
    StyledConv2DBlock, Conv2DBlock, ConstantInput2D, GANsynthInitFormatLayer
from utils.utils import num_flat_features
from .mini_batch_stddev_module import miniBatchStdDev
from .progressive_conv_net import DNet
from .styled_progressive_conv_net import StyledGNet
import random


class TStyledGNet(StyledGNet):
    """

    """
    def __init__(self, **kargs):
        r"""
        Build a generator for a progressive GAN model

        Args:

            - dimLatent (int): dimension of the latent vector
            - depthScale0 (int): depth of the lowest resolution scales
            - initBiasToZero (bool): should we set the bias to zero when a
                                    new scale is added
            - leakyReluLeak (float): leakyness of the leaky relu activation
                                    function
            - normalization (bool): normalize the input latent vector
            - generationActivation (function): activation function of the last
                                               layer (RGB layer). If None, then
                                               the identity is used
            - dimOutput (int): dimension of the output image. 3 -> RGB, 1 ->
                               grey levels
            - equalizedlR (bool): set to true to initiualize the layers with
                                  N(0,1) and apply He's constant at runtime

        """
        self.add_gradient_map = True
        StyledGNet.__init__(self, **kargs)

    def initFormatLayer(self):
        r"""
        The format layer represents the first weights applied to the latent
        vector. It converts a 1xdimLatent input into a 4 x 4 x scalesDepth[0]
        layer.
        """

        self.formatLayer = StyledConv2DBlock(
                                            in_channel=self.dimOutput,
                                            out_channel=self.scalesDepth[0],
                                            kernel_size=self.kernelSize, 
                                            padding=self.padding,
                                            style_dim=self.dimLatent,
                                            init_size=self.sizeScale0,
                                            transposed=self.transposed,
                                            noise_injection=self.noise_injection)

    def initStyleBlock(self):
        layers = [AudioNorm()]
        for i in range(self.n_mlp):
            layers.append(EqualizedLinear(self.dimLatent, self.dimLatent))

            layers.append(nn.LeakyReLU(0.2))

        self.style = nn.Sequential(*layers)

    def initScale0Layer(self):
        self.scaleLayers.append(StyledConv2DBlock(
                                        in_channel=self.scalesDepth[0],
                                        out_channel=self.scalesDepth[0],
                                        kernel_size=self.kernelSize, 
                                        padding=self.padding,
                                        style_dim=self.dimLatent,
                                        init_size=self.sizeScale0,
                                        transposed=self.transposed,
                                        noise_injection=self.noise_injection))

        self.toRGBLayers.append(EqualizedConv2d(self.scalesDepth[0], 
                                                self.dimOutput, 1,
                                                transposed=self.transposed,
                                                equalized=self.equalizedlR,
                                                initBiasToZero=self.initBiasToZero))

    def addScale(self, depthNewScale):
        r"""
        Add a new scale to the model. Increasing the output resolution by
        a factor 2

        Args:
            - depthNewScale (int): depth of each conv layer of the new scale
        """
        if type(depthNewScale) is list:
            depthNewScale = depthNewScale[0]
        depthLastScale = self.scalesDepth[-1]
        self.scalesDepth.append(depthNewScale) 
        self.scaleLayers.append(StyledConv2DBlock(
                                                in_channel=depthLastScale,
                                                out_channel=depthNewScale,
                                                kernel_size=self.kernelSize, 
                                                padding=self.padding,
                                                style_dim=self.dimLatent,
                                                init_size=self.sizeScale0,
                                                transposed=self.transposed,
                                                noise_injection=self.noise_injection))


        self.toRGBLayers.append(EqualizedConv2d(depthNewScale,
                                                self.dimOutput,
                                                1, 
                                                transposed=self.transposed,
                                                equalized=self.equalizedlR,
                                                initBiasToZero=self.initBiasToZero))

    def forward(self,
                input_z,
                input_x,
                noise=None, 
                scale=0, 
                mean_style=None, 
                style_weight=0):

        step = len(self.toRGBLayers) - 1
        style = self.style(input_z)
        batch_size = input_z.size(0)
        noise_dim = (batch_size, 
                     1, 
                     self.outputSizes[-1][0], 
                     self.outputSizes[-1][1])
        if noise is None:
            noise = []
            for i in range(step + 1):
                noise.append(torch.randn(noise_dim, device=input_z.device))
        
        out = self.formatLayer(input_x,
                               style=style,
                               noise=torch.randn(noise_dim, device=input_z.device))
        out = self.add_grad_map(out)
        for i, (conv, to_rgb) in enumerate(zip(self.scaleLayers, self.toRGBLayers)):
            out = conv(out, style, noise[i])
            out = self.add_grad_map(out)

        return to_rgb(out)

    def add_grad_map(self, x):
        if not self.add_gradient_map:
            return x
        # Adds a top-down gradient map (overwrites first map of x)
        grad = torch.linspace(0, 1, x.shape[2])
        if torch.cuda.is_available():
            grad = grad.cuda()

        x[:, 0:1, :, :] = grad[None, None, :, None]
        return x


    def mean_style(self, input):
        style = self.style(input).mean(0, keepdim=True)

        return style


class TStyledDNet(DNet):
    def __init__(self, **args):
        args['dimInput'] *= 2
        args['miniBatchNormalization'] = False
        DNet.__init__(self, **args)

    def initScale0Layer(self):
        # Minibatch standard deviation
        dimEntryScale0 = self.depthScale0
        self.fromRGBLayers.append(EqualizedConv2d(self.dimInput, self.depthScale0, 1,
                                                  equalized=self.equalizedlR,
                                                  initBiasToZero=self.initBiasToZero))
        self.groupScaleZero.append(EqualizedConv2d(dimEntryScale0, self.depthScale0,
                                                   self.kernelSize, padding=self.padding,
                                                   equalized=self.equalizedlR,
                                                   initBiasToZero=self.initBiasToZero))

        self.groupScaleZero.append(EqualizedLinear(self.inputSizes[0][0] * self.inputSizes[0][1] * self.depthScale0, # here we have to multiply times the initial size (8 for generating 4096 in 9 scales)
                                                   self.depthScale0,
                                                   equalized=self.equalizedlR,
                                                   initBiasToZero=self.initBiasToZero))

    def initDecisionLayer(self, sizeDecisionLayer):
        self.decisionLayer = EqualizedLinear(self.scalesDepth[0],
                                             sizeDecisionLayer,
                                             equalized=self.equalizedlR,
                                             initBiasToZero=self.initBiasToZero)

    def forward(self, x, getFeature = False):
        # From RGB layer
        x = self.leakyRelu(self.fromRGBLayers[-1](x))

        # Caution: we must explore the layers group in reverse order !
        # Explore all scales before 0

        shift = len(self.fromRGBLayers) - 2

        for i, groupLayer in enumerate(reversed(self.scaleLayers)):
            for layer in groupLayer:
                x = self.leakyRelu(layer(x))
            x = self.downScale(x, size=self.inputSizes[shift])
            shift -= 1
       
       # Now the scale 0
       # Minibatch standard deviation
        if self.miniBatchNormalization:
            x = miniBatchStdDev(x)

        x = self.leakyRelu(self.groupScaleZero[0](x))
        x = x.view(-1, num_flat_features(x))

        x_lin = self.groupScaleZero[1](x)
        x = self.leakyRelu(x_lin)

        out = self.decisionLayer(x)

        if not getFeature:
            return out

        return out, x_lin