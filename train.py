"""Training script"""

import os
import time
import argparse
from data_gen import DataLoader
import ipdb
import torch
import torch.nn as nn
from torch.autograd import Variable
from random import shuffle

from model import CNN
from model import EncoderBRNN
from model import AttnDecoderRNN

from utils import timeSince

use_cuda = torch.cuda.is_available()

# Create argument parser
parser = argparse.ArgumentParser()

# Data paths
parser.add_argument('--data_base_dir', type=str,
                    default='../data/images_processed',
                    help='Path of folder with processed images')
parser.add_argument('--label_path', type=str,
                    default='../data/im2latex_formulas.norm.lst',
                    help='Path of file with the latex formulas, one per line')
parser.add_argument('--train_path', type=str,
                    default="../data/im2latex_train_filter.lst",
                    help='Path of train file with the name of the image and its \
                          corresponding line number')
parser.add_argument('--vocabulary', type=str,
                    default='../data/latex_vocab.txt',
                    help='Path of file with the latex vocab, one per line')
parser.add_argument('--save_cnn', type=str,  default='cnn_model.pt',
                    help='path to save the CNN model')
parser.add_argument('--save_encoder', type=str,  default='encoder_model.pt',
                    help='path to save the BRNN encoder model')
parser.add_argument('--save_decoder', type=str,  default='decoder_model.pt',
                    help='path to save the attention decoder model')

# Max and mins for data generator
parser.add_argument('--max_aspect_ratio', type=float,
                    default=10, help='Maximum permitted aspect ratio of images')
parser.add_argument('--max_encoder_l_h', type=float, default=20,
                    help='Maximum permitted size for the image height')
parser.add_argument('--max_encoder_l_w', type=float, default=64,
                    help='Maximum permitted size for the image width')
parser.add_argument('--max_decoder_l', type=float, default=150,
                    help='Maximum permitted size (number of tokens) for the \
                          associated latex formula')

# Hyperparameters
parser.add_argument('--num_epochs', type=int, default=5,
                    help='Number of epochs for training')
parser.add_argument('--batch_size', type=int, default=5,
                    help='Batch size for training')
parser.add_argument('--learning_rate', type=float, default=0.001,
                    help='Initial learning rate for training')

# Encoder
parser.add_argument('--num_layers_encoder', type=int, default=5,
                    help='Number of layers in the bidirectional recurrent \
                          neural network used for encoding')
parser.add_argument('--hidden_dim_encoder', type=int, default=512)
parser.add_argument('--max_lenth_encoder', type=int, default=1000)

# Decoder
parser.add_argument('--output_dim_decoder', type=int, default=1000)
parser.add_argument('--num_layers_decoder', type=int, default=3)
parser.add_argument('--max_length_decoder', type=int, default=1000)

# parse arguments
args = parser.parse_args()


def train(images, targets, targets_eval, cnn, encoder, decoder, cnn_optimizer,
          encoder_optimizer, decoder_optimizer, criterion, max_length,
          use_cuda):
    cnn_optimizer.zero_grad()
    encoder_optimizer.zero_grad()
    decoder_optimizer.zero_grad()

    images = Variable(images)
    images = images.cuda() if use_cuda else images

    loss = 0

    # Forward Pass
    # Convolutional network
    list_rows = cnn(images)

    # Encoder
    outputs = encoder(list_rows)

    # Decoder
    encoder_outputs = Variable(torch.zeros(max_length, encoder.batch_size,
                               encoder.hidden_dim_encoder))
    encoder_outputs = encoder_outputs.cuda() if use_cuda else encoder_outputs

    # The encoder output should be inside the encoder_outputs of size
    # (max_length, batch_size, hidden_dim)
    encoder_outputs[:outputs.size(0)] = outputs

    # Calculate the first hidden vector of the decoder LSTM
    img_mean = encoder_outputs.sum(0)
    W1 = Variable(torch.zeros(512, 512))
    W1 = W1.cuda() if use_cuda else W1
    b1 = Variable(torch.zeros(512, 1))
    b1 = b1.cuda() if use_cuda else b1
    m = nn.Tanh()

    decoder_hidden = m(torch.mm(img_mean.squeeze(),
                                W1) +
                       b1.transpose(0,
                                    1).expand_as(torch.mm(img_mean.squeeze(),
                                                          W1)))

    # Calculate the first cell state of the decoder LSTM
    W2 = Variable(torch.zeros(512, 512))
    W2 = W2.cuda() if use_cuda else W2
    b2 = Variable(torch.zeros(512, 1))
    b2 = b2.cuda() if use_cuda else b2
    decoder_cell_state = m(torch.mm(img_mean.squeeze(), W2) +
                           b2.transpose(0,
                                        1).expand_as(torch.mm(
                                                     img_mean.squeeze(),
                                                     W2)))

    use_teacher_forcing = True

    if use_teacher_forcing:
        # teacher forcing: feed the target as the next input
        # save values for evaluation
        predicted_index = []
        actual_index = []

        for di in range(targets.size(1)):

            # Take the di-th target for each batch
            decoder_input = targets.narrow(1, di, 1)
            decoder_input = torch.LongTensor(decoder_input.numpy().astype(int))
            decoder_input = Variable(decoder_input)
            decoder_input = decoder_input.cuda() if use_cuda else decoder_input

            # Take the di-th target_eval for each batch
            decoder_eval = targets_eval.narrow(1, di, 1)
            decoder_eval = torch.LongTensor(decoder_eval.numpy().astype(int))
            decoder_eval = Variable(decoder_eval.squeeze())
            decoder_eval = decoder_eval.cuda() if use_cuda else decoder_eval

            decoder_output, decoder_hidden, decoder_cell_state = decoder(decoder_input,
                                                                         decoder_hidden,
                                                                         decoder_cell_state,
                                                                         encoder_outputs)

            loss += criterion(decoder_output, decoder_eval)

            # save values for evaluation
            if use_cuda:
                predicted_index.append(torch.max(decoder_output.data, 1)[1][0])
                actual_index.append(decoder_eval.data[0])
            else:
                predicted_index.append(torch.max(decoder_output.data,
                                                 1)[1][0][0])
                actual_index.append(decoder_eval.data[0])

        loss.backward()
        cnn_optimizer.step()
        encoder_optimizer.step()
        decoder_optimizer.step()

    return loss.data[0]/targets.size(1), predicted_index, actual_index


def trainIters(batch_size, cnn, encoder, decoder, data_loader,
               learning_rate, n_iters, print_every,
               use_cuda):
    start = time.time()
    print_losses = []
    print_loss_total = 0

    # Loss and optimizer
    cnn_optimizer = torch.optim.Adam(cnn.parameters(), lr=learning_rate)
    encoder_optimizer = torch.optim.Adam(encoder.parameters(),
                                         lr=learning_rate)
    decoder_optimizer = torch.optim.Adam(decoder.parameters(),
                                         lr=learning_rate)

    criterion = nn.NLLLoss()

    data_generator = data_loader.create_data_generator(args.batch_size,
                                                       args.train_path)

    best_loss = None

    for iter in range(1, n_iters+1):
        (images, targets, targets_eval,
         num_nonzer, img_paths) = data_generator.next()
        loss, predicted_index, actual_index = train(images, targets,
                                                    targets_eval, cnn,
                                                    encoder,
                                                    decoder, cnn_optimizer,
                                                    encoder_optimizer,
                                                    decoder_optimizer,
                                                    criterion,
                                                    args.max_lenth_encoder,
                                                    use_cuda)

        print_loss_total += loss

        if iter % print_every == 0:
            print_loss_avg = print_loss_total / print_every
            print_losses.append(print_loss_avg)
            print_loss_total = 0

            print('%s (%d %d%%) %.4f' % (timeSince(start, iter/float(n_iters)),
                                         iter, iter / float(n_iters) * 100,
                                         print_loss_avg))

            print("Predicted Tokens")
            print([data_loader.tokenizer.id2vocab[i] for i in predicted_index])
            print("Actual Tokens")
            print([data_loader.tokenizer.id2vocab[i] for i in actual_index])

        if not best_loss or print_loss_avg < best_loss:
            with open(args.save_cnn, 'wb') as f:
                torch.save(cnn, f)
            with open(args.save_encoder, 'wb') as f:
                torch.save(encoder, f)
            with open(args.save_decoder, 'wb') as f:
                torch.save(decoder, f)

    return print_losses


# Create data loader
dataloader = DataLoader(args.data_base_dir, args.label_path,
                        args.max_aspect_ratio, args.max_encoder_l_h,
                        args.max_encoder_l_w, args.max_decoder_l)

# Create the modules of the algorithm
cnn1 = CNN()
encoder1 = EncoderBRNN(args.batch_size, args.num_layers_encoder,
                       args.hidden_dim_encoder, use_cuda)
decoder1 = AttnDecoderRNN(args.hidden_dim_encoder//2, args.output_dim_decoder,
                          args.num_layers_decoder, args.max_length_decoder,
                          dataloader.vocab_size)

if use_cuda:
    cnn1 = cnn1.cuda()
    encoder1 = encoder1.cuda()
    decoder1 = decoder1.cuda()

trainIters(args.batch_size, cnn1, encoder1, decoder1, dataloader,
           args.learning_rate, n_iters=75000, print_every=10,
           use_cuda=use_cuda)
