# %%
import torch
import torch.nn.functional as F
import torchvision
import numpy as np
import pandas as pd
import random
import os
import copy
import PIL
import argparse

from tqdm import tqdm
from sklearn.model_selection import train_test_split
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import confusion_matrix

from utils.config import base_path
from utils import vars
from utils import trainer
from utils import metrics
from utils import utils

import matplotlib.pyplot as plt
import seaborn as sns

from models import covid_classifier
from models import pneumonia_classifier

from datasets import corda

import functools

# %%
seed = vars.seed
device = torch.device('cuda:0')

# %%
parser = argparse.ArgumentParser()
parser.add_argument('--arch', type=str, default='resnet18', help='encoder architecture (resnet18 or resnet50)')
parser.add_argument('--pretrain', type=str, default='chestxray', help='pretrained (chestxray, rsna, none)')
parser.add_argument('--train', type=str, default='corda', help='corda, corda+chest, corda+rsna, corda+cohen, cohen')
args = parser.parse_args()


# %%
device = torch.device('cuda:0')
utils.set_seed(seed)

model_preprocessed = 'equalized'
feature_preprocessed = '-eq+masked'
preprocessed = '(preprocessed)'

# %%
corda_dataset = 'CORDA-dataset-v4-equalized+masked'
corda_version = f'CORDA-dataset-{vars.corda_version}'
corda_basepath = os.path.join(base_path, 'corda', corda_version, corda_dataset)
corda_df = pd.read_csv(os.path.join(corda_basepath, 'CORDA_fix.csv'))
corda_train_df, corda_test_df = train_test_split(corda_df, test_size=0.3, random_state=vars.seed, stratify=corda_df.covid)
corda_train_df, corda_val_df = train_test_split(corda_train_df, test_size=0.2, random_state=vars.seed, stratify=corda_train_df.covid)


# %%
rsna_dataset = 'rsna_bal_subset-equalized+masked'
rsna_basepath = os.path.join(base_path, rsna_dataset)
rsna_df = pd.read_csv(os.path.join(rsna_basepath, 'stage_2_train_labels_subset.csv'))
rsna_train_df = corda.preprocess_rsna_df(rsna_df)
rsna_train_df, rsna_test_df = train_test_split(rsna_df, test_size=0.3, random_state=vars.seed, stratify=rsna_train_df.label)
rsna_train_df, rsna_val_df = train_test_split(rsna_train_df, test_size=0.2, random_state=vars.seed, stratify=rsna_train_df.label)

# %%
chestxray_dataset = 'chest_xray-equalized+masked'
chestxray_basepath = os.path.join(base_path, chestxray_dataset)
chestxray_train_df = pd.read_csv(os.path.join(chestxray_basepath, 'train_3_classes.csv'))
chestxray_val_df = pd.read_csv(os.path.join(chestxray_basepath, 'val_3_classes.csv'))
chestxray_test_df = pd.read_csv(os.path.join(chestxray_basepath, 'test_3_classes.csv'))
chestxray_train_df = corda.preprocess_chest_df(chestxray_train_df)
chestxray_val_df = corda.preprocess_chest_df(chestxray_val_df)
chestxray_test_df = corda.preprocess_chest_df(chestxray_test_df)

# %%
cohen_dataset = 'cohen-equalized+masked'
cohen_basepath = os.path.join(base_path, cohen_dataset)
cohen_train_df = pd.read_csv(os.path.join(cohen_basepath, 'train.csv'))
cohen_test_df = pd.read_csv(os.path.join(cohen_basepath, 'test.csv'))
cohen_train_df = corda.preprocess_cohen_df(cohen_train_df)
cohen_test_df = corda.preprocess_cohen_df(cohen_test_df)
cohen_train_df, cohen_val_df = train_test_split(cohen_train_df, test_size=0.2, random_state=vars.seed, stratify=cohen_train_df.covid)

# %%
def balance_corda_with_other(corda_df, other_df):
    covid1_size = len(corda_df[corda_df.covid == 1])
    covid0_size = len(corda_df[corda_df.covid == 0])
    delta = covid1_size - covid0_size
    corda_df = pd.concat((corda_df, other_df.sample(n=delta, random_state=vars.seed).copy()))
    return corda_df

# %% MEAN & STD
encoder_df = pd.concat((corda_train_df, corda_val_df))

if args.pretrain == 'chestxray':
    encoder_df = pd.concat((encoder_df, chestxray_train_df, chestxray_val_df))

elif args.pretrain == 'rsna':
    encoder_df = pd.concat((encoder_df, rsna_train_df))

elif args.pretrain == 'none':
    pass

else:
    print(f'Unkown pretrain value: {args.pretrain}')
    exit(1)

stats_transforms = torchvision.transforms.Compose([
    torchvision.transforms.Resize(256),
    torchvision.transforms.CenterCrop(224),
    torchvision.transforms.ToTensor(),
])

stats_dataset = corda.CORDA(
    encoder_df,
    corda_base_path=corda_basepath,
    rsna_base_path=rsna_basepath,
    chest_base_path=chestxray_basepath,
    transform=stats_transforms
)

stats_dataloader = torch.utils.data.DataLoader(
    stats_dataset, batch_size=10,
    shuffle=False, num_workers=10,
    worker_init_fn=lambda id: utils.set_seed(seed),
    pin_memory=True
)

mean, std = utils.get_mean_and_std(stats_dataloader)
print(f'Mean & std for corda+{args.pretrain}:', mean, std)



# CORDA ONLY (balance majority class)
train_df = corda_train_df
val_df = corda_val_df
test_df = corda_test_df

if args.train == 'corda':
    train_df = pd.concat((
        corda_train_df[corda_train_df.covid == 0].sample(n=84, random_state=42),
        corda_train_df[corda_train_df.covid == 1].sample(n=84, random_state=42)
    ))

elif args.train == 'corda+chest':
    train_df = balance_corda_with_other(corda_train_df, chestxray_train_df)
    val_df = balance_corda_with_other(corda_val_df, chestxray_val_df)
    test_df = balance_corda_with_other(corda_test_df, chestxray_test_df)

elif args.train == 'corda+rsna':
    train_df = balance_corda_with_other(corda_train_df, rsna_train_df)
    val_df = balance_corda_with_other(corda_val_df, rsna_val_df)
    test_df = balance_corda_with_other(corda_test_df, rsna_test_df)

elif args.train == 'corda+cohen':
    train_df = pd.concat((train_df, cohen_train_df))
    val_df = pd.concat((val_df, cohen_val_df))
    test_df = pd.concat((test_df, cohen_test_df))

    noncovid_size = len(train_df[train_df.covid == 0])
    train_df = pd.concat((
        train_df[train_df.covid == 0].sample(n=noncovid_size, random_state=42),
        train_df[train_df.covid == 1].sample(n=noncovid_size, random_state=42)
    ))

elif args.train == 'cohen':
    train_df = cohen_train_df
    val_df = cohen_val_df
    test_df = cohen_test_df

else:
    print(f'Unknown train mode: {args.train}')
    exit(1)



# %%
train_transforms = torchvision.transforms.Compose([
    torchvision.transforms.Resize(256),
    torchvision.transforms.RandomHorizontalFlip(p=0.2),
    torchvision.transforms.RandomAffine((-1, 1), translate=(0, 0.1), scale=(1, 1.1)),
    torchvision.transforms.CenterCrop(224),
    torchvision.transforms.ToTensor(),
    torchvision.transforms.Normalize(mean, std)
])

transforms = torchvision.transforms.Compose([
    torchvision.transforms.Resize(256),
    torchvision.transforms.CenterCrop(224),
    torchvision.transforms.ToTensor(),
    torchvision.transforms.Normalize(mean, std),
])


# %%
train_dataset = corda.CORDA(
    train_df,
    corda_base_path=corda_basepath,
    chest_base_path=chestxray_basepath,
    rsna_base_path=rsna_basepath,
    cohen_base_path=cohen_basepath,
    transform=train_transforms
)

val_dataset = corda.CORDA(
    val_df,
    corda_base_path=corda_basepath,
    chest_base_path=chestxray_basepath,
    rsna_base_path=rsna_basepath,
    cohen_base_path=cohen_basepath,
    transform=transforms
)

test_dataset = corda.CORDA(
    test_df,
    corda_base_path=corda_basepath,
    chest_base_path=chestxray_basepath,
    rsna_base_path=rsna_basepath,
    cohen_base_path=cohen_basepath,
    transform=transforms
)

train_dataloader = torch.utils.data.DataLoader(
    train_dataset, batch_size=4,
    shuffle=True, num_workers=0,
    worker_init_fn=lambda id: utils.set_seed(seed),
    pin_memory=True
)

val_dataloader = torch.utils.data.DataLoader(
    val_dataset, batch_size=10,
    shuffle=False, num_workers=4,
    worker_init_fn=lambda id: utils.set_seed(seed+id),
    pin_memory=True
)

test_dataloader = torch.utils.data.DataLoader(
    test_dataset, batch_size=10,
    shuffle=False, num_workers=4,
    worker_init_fn=lambda id: utils.set_seed(seed+id)
)

# %%
if args.arch not in ['resnet18', 'resnet50']:
    print(f'Unkown arch {args.arch}')
    exit(0)

name = f'{args.arch}/{args.pretrain}/{args.train}'
utils.ensure_dir(f'logs/{vars.corda_version}/{name}')
utils.ensure_dir(f'models/{vars.corda_version}/{args.arch}/{args.pretrain}')

# MODEL CREATION
# %%


if args.arch == 'resnet18':
    model = covid_classifier.CovidClassifier(
        encoder=None,
        pretrained=False,
        freeze_conv=False
    )
elif args.arch == 'resnet50':
    model = covid_classifier.CovidClassifier50(
        encoder=None,
        pretrained=False,
        freeze_conv=False
    )

checkpoint = torch.load(f'models/{vars.corda_version}/{name}.pt', map_location={'cuda:0': 'cpu'})
model.load_state_dict(checkpoint["model"])
model = model.to(device)
print(f'Loaded {name} from epoch {checkpoint["epoch"]}')

#model = covid_classifier.LeNet1024NoPoolingDeep().to(device)


# TRAINING
# %%
tracked_metrics = [
    metrics.Accuracy(),
    metrics.RocAuc(),
    metrics.FScore()
]

def focal_loss(output, target, gamma=2., weight=None):
    bce = F.binary_cross_entropy(output, target, reduction='none', weight=weight)
    pt = target*output + (1-target)*(1-output)
    return (torch.pow((1-pt), gamma) * bce).mean()

criterion = focal_loss

print(f'Benchmarking model..')

corda_test_dataset = corda.CORDA(
    corda_test_df,
    corda_base_path=corda_basepath,
    transform=transforms
)

corda_rxpos_dataset = corda.CORDA(
    corda_test_df[corda_test_df.rx == 1],
    corda_base_path=corda_basepath,
    transform=transforms
)

corda_rxneg_dataset = corda.CORDA(
    corda_test_df[corda_test_df.rx == 0],
    corda_base_path=corda_basepath,
    transform=transforms
)

corda_chest_dataset = corda.CORDA(
    pd.concat((corda_test_df, chestxray_test_df)),
    corda_base_path=corda_basepath,
    chest_base_path=chestxray_basepath,
    transform=transforms
)

corda_rsna_dataset = corda.CORDA(
    pd.concat((corda_test_df, rsna_test_df)),
    corda_base_path=corda_basepath,
    rsna_base_path=rsna_basepath,
    transform=transforms
)

corda_cohen_dataset = corda.CORDA(
    pd.concat((corda_test_df, cohen_test_df)),
    corda_base_path=corda_basepath,
    cohen_base_path=cohen_basepath,
    transform=transforms
)

rsna_test_dataset = corda.CORDA(
    rsna_test_df,
    rsna_base_path=rsna_basepath,
    transform=transforms
)

rxpos_dataset = corda.CORDA(
    test_df[test_df.rx == 1],
    corda_base_path=corda_basepath,
    chest_base_path=chestxray_basepath,
    rsna_base_path=rsna_basepath,
    cohen_base_path=cohen_basepath,
    transform=transforms
)

chest_test_dataset = corda.CORDA(
    chestxray_test_df,
    chest_base_path=chestxray_basepath,
    transform=transforms
)

cohen_all_dataset = corda.CORDA(
    pd.concat((cohen_train_df, cohen_val_df, cohen_test_df)),
    cohen_base_path=cohen_basepath,
    transform=transforms
)

cohen_test_dataset = corda.CORDA(
    cohen_test_df,
    cohen_base_path=cohen_basepath,
    transform=transforms
)


benchmark_data = {
    'arch': [], 'pretrain': [], 'train': [],
    'test': [], 'accuracy': [], 'auc': [],
    'sensitivity': [], 'specificity': [], 'fscore': [],
    'ba': [], 'missrate': [], 'dor': []
}

def benchmark_dataset(dataset, title, fname, testname, xlabels, ylabels=None):
    global benchmark_data
    print(f'Benchmarking {title}.. ')

    dataloader = torch.utils.data.DataLoader(
        dataset, batch_size=10,
        shuffle=False, num_workers=4,
    )

    tracked_metrics = [
        metrics.Accuracy(),
        metrics.RocAuc(),
        metrics.FScore()
    ]

    logs, cm = trainer.test(
        model=model, test_dataloader=dataloader,
        criterion=criterion, metrics=tracked_metrics, device=device
    )

    with open(f'logs/{vars.corda_version}/{name}/{fname}-metric.txt', 'w') as f:
        f.write(f'{fname}: ' + trainer.summarize_metrics(logs) + '\n')

    ax = sns.heatmap(
        cm.get(normalized=True), annot=True, fmt=".2f",
        xticklabels=xlabels, yticklabels=ylabels or xlabels,
        vmin=0., vmax=1.
    )
    ax.set_title(title)
    plt.xlabel('predicted')
    plt.ylabel('ground')
    hm = ax.get_figure()
    hm.savefig(f'logs/{vars.corda_version}/{name}/{fname}.png')
    hm.clf()

    fpr, tpr, thresholds = tracked_metrics[1].get_curve()
    auc = tracked_metrics[1].get()
    f = plt.figure()
    plt.plot(fpr, tpr, color='darkorange', lw=2, label=f'ROC curve (auc = {auc:.2f})')
    plt.title(f'{title} ROC')
    plt.legend(loc='lower right')
    plt.savefig(f'logs/{vars.corda_version}/{name}/{fname}-roc.png')
    plt.clf()
    plt.cla()
    plt.close()

    specificity, fpr, fnr, sensitivity = cm.get(normalized=True).ravel()
    dor = (sensitivity*specificity)/((1-sensitivity)*(1-specificity))
    fscore = tracked_metrics[2].get()
    ba = (sensitivity+specificity)/2.

    data = {
        'arch': args.arch, 'pretrain': args.pretrain, 'train': args.train.upper(),
        'test': testname, 'accuracy': tracked_metrics[0].get(), 'auc': auc,
        'sensitivity': sensitivity, 'specificity': specificity, 'fscore': fscore,
        'ba': ba, 'missrate': fnr, 'dor': dor
    }

    for k,v in data.items():
        benchmark_data[k].append(v)



benchmark_dataset(corda_test_dataset, f'CORDA {preprocessed}', 'corda', 'CORDA', ['covid-', 'covid+'])
benchmark_dataset(corda_rxpos_dataset, f'CORDA RX+ {preprocessed}', 'corda-rx+', 'CORDA RX+', ['covid-', 'covid+'])
benchmark_dataset(corda_rxneg_dataset, f'CORDA RX- {preprocessed}', 'corda-rx-', 'CORDA RX-', ['covid-', 'covid+'])
benchmark_dataset(rxpos_dataset, f'Test {args.train.upper()} RX+ {preprocessed}', f'test-{args.train}-rx+',  f'{args.train.upper()} RX+', ['covid-', 'covid+'])
benchmark_dataset(cohen_all_dataset, f'Cohen (All) {preprocessed}', 'cohen-all', 'Cohen (All)', ['covid-', 'covid+'])
benchmark_dataset(cohen_test_dataset, f'Cohen (Test) {preprocessed}', 'cohen-test', 'Cohen', ['covid-', 'covid+'])
benchmark_dataset(corda_chest_dataset, f'CORDA+ChestXRay (Test) {preprocessed}', 'corda-chest', 'CORDA+ChestXRay', ['covid-', 'covid+'])
benchmark_dataset(corda_rsna_dataset, f'CORDA+RSNA (Test) {preprocessed}', 'corda-rsna', 'CORDA+RSNA', ['covid-', 'covid+'])
benchmark_dataset(corda_cohen_dataset, f'CORDA+Cohen (Test) {preprocessed}', 'corda-cohen', 'CORDA+Cohen', ['covid-', 'covid+'])

rsna_test_df['covid'] = rsna_test_df['rx']
benchmark_dataset(rsna_test_dataset, f'RSNA {preprocessed}', 'rsna', 'RSNA', ['covid-', 'covid+'], ['rx-', 'rx+'])

chestxray_test_df['covid'] = chestxray_test_df['rx']
benchmark_dataset(chest_test_dataset, f'ChestXRay {preprocessed}', 'chestxray', 'ChestXRay', ['covid-', 'covid+'], ['rx-', 'rx+'])

pd.DataFrame(benchmark_data).to_csv(f'logs/{vars.corda_version}/{name}/benchmark.csv')
