from torchvision import transforms
import torch
import torch.optim as optim
from torch.autograd import Variable
from torch.utils.data import DataLoader
from torchvision import models
import os, time
import numpy as np
import pickle
from datetime import datetime

import utils.DataProcessing as DP
import utils.CalcHammingRanking as CalcHR

import CNN_model

def LoadLabel(filename, DATA_DIR):
    path = os.path.join(DATA_DIR, filename)
    fp = open(path, 'r')
    labels = [x.strip() for x in fp]
    fp.close()
    return torch.LongTensor(list(map(int, labels)))

def EncodingOnehot(target, nclasses):
    #print(target.shape)
    target_onehot = torch.FloatTensor(target.size(0), nclasses)

    target_onehot.zero_()
    target_onehot.scatter_(1, target.view(-1, 1), 1)
    return target_onehot

def CalcSim(batch_label, train_label):
    S = (batch_label.mm(train_label.t()) > 0).type(torch.FloatTensor)
    return S

def CreateModel(model_name, bit, use_gpu):
    if model_name == 'vgg11':
        vgg11_d = models.vgg11(pretrained=True)
        cnn_model = CNN_model.cnn_model(vgg11_d, model_name, bit)
    if model_name == 'vgg16':
        vgg16 = models.vgg16(pretrained = True)
        cnn_model = CNN_model.cnn_model(vgg16, model_name, bit)
    if model_name == 'alexnet':
        alexnet = models.alexnet(pretrained=True)
        cnn_model = CNN_model.cnn_model(alexnet, model_name, bit)
    if use_gpu:
        cnn_model = cnn_model.cuda()
    return cnn_model

def AdjustLearningRate(optimizer, epoch, learning_rate):
    lr = learning_rate * (0.1 ** (epoch // 50))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return optimizer

def GenerateCode(model, data_loader, num_data, bit, use_gpu):
    B = np.zeros([num_data, bit], dtype=np.float32)
    for iter, data in enumerate(data_loader, 0):
        data_input, _, data_ind = data
        if use_gpu:
            data_input = Variable(data_input.cuda())
        else:
	    data_input = Variable(data_input)
        output = model(data_input)
        if use_gpu:
            B[data_ind.numpy(), :] = torch.sign(output.cpu().data).numpy()
        else:
            B[data_ind.numpy(), :] = torch.sign(output.data).numpy()
    return B

def Logtrick(x, use_gpu):
    if use_gpu:
        lt = torch.log(1+torch.exp(-torch.abs(x))) + torch.max(x, Variable(torch.FloatTensor([0.]).cuda()))
    else:
        lt = torch.log(1+torch.exp(-torch.abs(x))) + torch.max(x, Variable(torch.FloatTensor([0.])))
    return lt

def Totloss(U, B, Sim, lamda, num_train):
    theta = U.mm(U.t()) / 2
    t1 = (theta*theta).sum() / (num_train * num_train)
    l1 = (- theta * Sim + Logtrick(Variable(theta), False).data).sum()
    l2 = (U - B).pow(2).sum()
    l = l1 + lamda * l2
    return l, l1, l2, t1

def DPSH_algo(bit, param, gpu_ind=0):
    # parameters setting
    #os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu_ind)

    DATA_DIR = '/home/kfxw/Development/data/Retrieval/cifar10_retrieval'
    DATABASE_FILE = 'database_img.txt'
    TRAIN_FILE = 'train_img.txt'
    TEST_FILE = 'test_img.txt'

    DATABASE_LABEL = 'database_label.txt'
    TRAIN_LABEL = 'train_label.txt'
    TEST_LABEL = 'test_label.txt'

    batch_size = 125
    epochs = 150
    learning_rate = 0.05
    weight_decay = 10 ** -5
    model_name = 'vgg16'
    nclasses = 10
    use_gpu = torch.cuda.is_available()
    iter_size = 5
    if batch_size < iter_size or batch_size % iter_size != 0:
	print "Batch size must be an interge multiple of iter size. batch_size vs. iter_size: {%d,%d}".format(batch_size, iter_size)
	return
    iter_batch = batch_size / iter_size

    filename = param['filename']
    lamda = param['lambda']
    param['bit'] = bit
    param['epochs'] = epochs
    param['learning rate'] = learning_rate
    param['model'] = model_name

    ### data processing
    transformations = transforms.Compose([
        transforms.Scale(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ])
    dset_database = DP.DatasetProcessingCIFAR_10(
        DATA_DIR, DATABASE_FILE, DATABASE_LABEL, transformations)

    dset_train = DP.DatasetProcessingCIFAR_10(
        DATA_DIR, TRAIN_FILE, TRAIN_LABEL, transformations)

    dset_test = DP.DatasetProcessingCIFAR_10(
        DATA_DIR, TEST_FILE, TEST_LABEL, transformations)

    num_database, num_train, num_test = len(dset_database), len(dset_train), len(dset_test)

    database_loader = DataLoader(dset_database,
                              batch_size=1,
                              shuffle=False,
                              num_workers=4
                             )

    train_loader = DataLoader(dset_train,
                              batch_size=batch_size,
                              shuffle=True,
                              num_workers=2
                             )

    test_loader = DataLoader(dset_test,
                             batch_size=1,
                             shuffle=False,
                             num_workers=4
                             )

    ### create model
    model = CreateModel(model_name, bit, use_gpu)
    optimizer = optim.SGD(
        model.parameters(),
        lr=learning_rate,
        weight_decay=weight_decay)

    ### training phase
    # parameters setting
    B_all = torch.zeros(num_train, bit)
    U_all = torch.zeros(num_train, bit)
    train_labels = LoadLabel(TRAIN_LABEL, DATA_DIR)
    train_labels_onehot = EncodingOnehot(train_labels, nclasses)
    test_labels = LoadLabel(TEST_LABEL, DATA_DIR)
    test_labels_onehot = EncodingOnehot(test_labels, nclasses)

    train_loss = []
    map_record = []

    totloss_record = []
    totl1_record = []
    totl2_record = []
    t1_record = []

    Sim = CalcSim(train_labels_onehot, train_labels_onehot)
    logfile = open('logs/'+filename.split('/')[-1].replace('.pkl','.log'),'a')
    ## training epoch
    for epoch in range(epochs):
        epoch_loss = 0.0
        ## training batch
        for iter, traindata in enumerate(train_loader, 0):
	    timer = time.time()
	    B = torch.zeros(batch_size, bit)
	    U = torch.zeros(batch_size, bit)
            train_input, train_label, batch_ind = traindata
            train_label = torch.squeeze(train_label)
	    # 1 get S matrix within the batch
            if use_gpu:
                train_label_onehot = EncodingOnehot(train_label, nclasses)
                train_input, train_label = Variable(train_input.cuda()), Variable(train_label.cuda())
                S = CalcSim(train_label_onehot, train_label_onehot)
            else:
                train_label_onehot = EncodingOnehot(train_label, nclasses)
                train_input, train_label = Variable(train_input), Variable(train_label)
                S = CalcSim(train_label_onehot, train_label_onehot)

            model.zero_grad()
	    # 2 forward for iter_size times to gather outputs
	    for iter_id in range(iter_size):
		output_idx = iter_id*iter_batch
        	train_outputs = model(train_input[output_idx : output_idx+iter_batch, ...])
                U[output_idx : output_idx+iter_batch, :] = train_outputs.data[...]
                B[output_idx : output_idx+iter_batch, :] = torch.sign(train_outputs.data[...])

	    # 3 restore U_all and B_all to calculate totloss
            for i, ind in enumerate(batch_ind):
                U_all[ind, :] = U[i,:]
                B_all[ind, :] = B[i,:]

	    # 4 forward and the backward iter_size times
	    for iter_id in range(iter_size):
		output_idx = iter_id*iter_batch
        	train_outputs = model(train_input[output_idx : output_idx+iter_batch, ...])
		Bbatch = torch.sign(train_outputs)
		# calculate loss over the iter_batch
		S_sub = S[output_idx:output_idx+iter_batch,:]
                if use_gpu:
                    theta_x = train_outputs.mm(Variable(U.cuda()).t()) / 2
                    logloss = (Variable(S_sub.cuda())*theta_x - Logtrick(theta_x, use_gpu)).sum()
                    regterm = (Bbatch-train_outputs).pow(2).sum()
                else:
                    theta_x = train_outputs.mm(Variable(U).t()) / 2
                    logloss = (Variable(S_sub)*theta_x - Logtrick(theta_x, use_gpu)).sum()
                    regterm = (Bbatch-train_outputs).pow(2).sum() 
                loss =  - (logloss + lamda * regterm) / (batch_size ** 2)
                loss.backward()			# accumulate weight gradients
		epoch_loss += loss.data[0]	# accumulate epoch loss

	    # 5 update weights over a batch
            optimizer.step()
	    print '[Iteration %d][%3.2fs/iter]' % (iter, time.time()-timer)

        ## end of training batch ##
        print '[Train Phase][Epoch: %3d/%3d][Loss: %3.5f]' % (epoch+1, epochs, epoch_loss / len(train_loader))

        optimizer = AdjustLearningRate(optimizer, epoch, learning_rate)

        l, l1, l2, t1 = Totloss(U_all, B_all, Sim, lamda, num_train)
        totloss_record.append(l)
        totl1_record.append(l1)
        totl2_record.append(l2)
        t1_record.append(t1)
        print '[Total Loss: %10.5f][total L1: %10.5f][total L2: %10.5f][norm theta: %3.5f]' % (l, l1, l2, t1)

        ### testing during epoch
        qB = GenerateCode(model, test_loader, num_test, bit, use_gpu)
        tB = torch.sign(B_all).numpy()
        map_ = CalcHR.CalcMap(qB, tB, test_labels_onehot.numpy(), train_labels_onehot.numpy())
        train_loss.append(epoch_loss / len(train_loader))
        map_record.append(map_)
        
        logfile.write(str(train_loss[-1])+'  '+str(map_)+'\n')
        
        print('[Test Phase ][Epoch: %3d/%3d] MAP(retrieval train): %3.5f' % (epoch+1, epochs, map_))
        #print(len(train_loader))
    ## end of training epoch ##

    ### evaluation phase
    ## create binary code
    model.eval()
    database_labels = LoadLabel(DATABASE_LABEL, DATA_DIR)
    database_labels_onehot = EncodingOnehot(database_labels, nclasses)
    qB = GenerateCode(model, test_loader, num_test, bit, use_gpu)
    dB = GenerateCode(model, database_loader, num_database, bit, use_gpu)

    map = CalcHR.CalcMap(qB, dB, test_labels_onehot.numpy(), database_labels_onehot.numpy())
    print('[Retrieval Phase] MAP(retrieval database): %3.5f' % map)
    ## end of evaluation ##

    result = {}
    result['qB'] = qB
    result['dB'] = dB
    result['train loss'] = train_loss
    result['map record'] = map_record
    result['map'] = map
    result['param'] = param
    result['total loss'] = totloss_record
    result['l1 loss'] = totl1_record
    result['l2 loss'] = totl2_record
    result['norm theta'] = t1_record
    result['filename'] = filename

    return result

if __name__=='__main__':
    bit = 12
    lamda = 50
    gpu_ind = 0
    filename = 'snapshots/DPSH_' + str(bit) + 'bits_cifar10_' + datetime.now().strftime("%y-%m-%d-%H-%M-%S") + '.pkl'
    param = {}
    param['lambda'] = lamda
    param['filename'] = filename
    result = DPSH_algo(bit, param, gpu_ind)
    fp = open(result['filename'], 'wb')
    pickle.dump(result, fp)
    fp.close()