import os
import pickle
from contextlib import nullcontext
import torch
import tiktoken
from model import GPTConfig, GPT

# -----------------------------------------------------------------------------

kernel_config=0
init_from = 'resume' # either 'resume' (from an out_dir) or a gpt2 variant (e.g. 'gpt2-xl')

config_kernel = {
    0: "baseline",
    1: "polynomial",
    2: "periodic",
    3: "gaussian"
}

out_dir = 'out-arc-' + config_kernel[kernel_config] # ignored if init_from is not 'resume'
start = "\n" # or "<|endoftext|>" or etc. Can also specify a file, use as: "FILE:prompt.txt"


temperature = 0.8 # 1.0 = no change, < 1.0 = less random, > 1.0 = more random, in predictions
top_k = 200 # retain only the top_k most likely tokens, clamp others to have 0 probability
seed = 1337
device = 'cuda' # examples: 'cpu', 'cuda', 'cuda:0', 'cuda:1', etc.
dtype = 'bfloat16' if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else 'float16' # 'float32' or 'bfloat16' or 'float16'
compile = False # use PyTorch 2.0 to compile the model to be faster

sparse = 0.


exec(open('configurator.py').read()) # overrides from command line or config file


# -----------------------------------------------------------------------------
config_kernel = {
    0: "baseline",
    1: "polynomial",
    2: "periodic",
    3: "gaussian"
}

out_dir = 'out-arc-' + config_kernel[kernel_config]
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)
torch.backends.cuda.matmul.allow_tf32 = True # allow tf32 on matmul
torch.backends.cudnn.allow_tf32 = True # allow tf32 on cudnn
device_type = 'cuda' if 'cuda' in device else 'cpu' # for later use in torch.autocast
ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[dtype]
ctx = nullcontext() if device_type == 'cpu' else torch.amp.autocast(device_type=device_type, dtype=ptdtype)

# model
if init_from == 'resume':
    # init from a model saved in a specific directory
    ckpt_path = os.path.join(out_dir, 'ckpt.pt')
    checkpoint = torch.load(ckpt_path, map_location=device)
    gptconf = GPTConfig(**checkpoint['model_args'])
    model = GPT(gptconf)
    state_dict = checkpoint['model']
    unwanted_prefix = '_orig_mod.'
    for k,v in list(state_dict.items()):
        if k.startswith(unwanted_prefix):
            state_dict[k[len(unwanted_prefix):]] = state_dict.pop(k)
    model.load_state_dict(state_dict)
elif init_from.startswith('gpt2'):
    # init from a given GPT-2 model
    model = GPT.from_pretrained(init_from, dict(dropout=0.0))



model.eval()
model.to(device)
if compile:
    model = torch.compile(model) # requires PyTorch 2.0 (optional)

# look for the meta pickle in case it is available in the dataset folder
load_meta = False
if init_from == 'resume' and 'config' in checkpoint and 'dataset' in checkpoint['config']: # older checkpoints might not have these...
    meta_path = os.path.join('data', checkpoint['config']['dataset'], 'meta.pkl')
    load_meta = os.path.exists(meta_path)
if load_meta:
    print(f"Loading meta from {meta_path}...")
    with open(meta_path, 'rb') as f:
        meta = pickle.load(f)
    # TODO want to make this more general to arbitrary encoder/decoder schemes
    stoi, itos = meta['stoi'], meta['itos']
    encode = lambda s: [stoi[c] for c in s]
    decode = lambda l: ''.join([itos[i] for i in l])
else:
    # ok let's assume gpt-2 encodings by default
    print("No meta.pkl found, assuming GPT-2 encodings...")
    enc = tiktoken.get_encoding("gpt2")
    encode = lambda s: enc.encode(s, allowed_special={"<|endoftext|>"})
    decode = lambda l: enc.decode(l)



import pandas as pd
from tqdm import tqdm

question_dirs = {"easy":["data/arc/ARC-V1-Feb2018-2/ARC-Easy/ARC-Easy-Train.csv", 
                        "data/arc/ARC-V1-Feb2018-2/ARC-Easy/ARC-Easy-Dev.csv",
                        "data/arc/ARC-V1-Feb2018-2/ARC-Easy/ARC-Easy-Test.csv"],
                "challenge":["data/arc/ARC-V1-Feb2018-2/ARC-Challenge/ARC-Challenge-Train.csv",
                             "data/arc/ARC-V1-Feb2018-2/ARC-Challenge/ARC-Challenge-Dev.csv",
                             "data/arc/ARC-V1-Feb2018-2/ARC-Challenge/ARC-Challenge-Test.csv"]
                }

accuracy = {"easy": {"total": 0, "correct": 0},
            "challenge": {"total": 0, "correct": 0}
            }


def generate_prompt(q, devs):
    prompt = "The following are multiple choice questions. There is only one correct option for each question.\n\n"
    for d in devs:
        prompt += d[0] + "\nThe answer (one letter) is:" + d[1] +"\n\n"
    prompt += q + "\nThe answer (one letter) is:"
    return prompt


def ans(question, devs):
    """ Input a question, return the answer (one character) from GPT2 model """
    prompt = generate_prompt(question, devs)
    start_ids = encode(prompt)
    x = (torch.tensor(start_ids, dtype=torch.long, device=device)[None, ...])
    with torch.no_grad():
        with ctx:
            y = model.generate(x, 1, temperature=temperature, top_k=top_k)
    answer = decode(y[0].tolist())[-2:] # Last letter
    return answer.upper()


def isCorrect(question, devs, answer):
    """ Input a question, return 1 if the answer is correct, else 0 """
    a = ans(question, devs)
    isCharacter = a[0]
    pre_ans = a[1]
    return 1 if not isCharacter.isalpha() and pre_ans == answer else 0


def update_accuracy(dir, difficulty):
    """ Input the directory of question file, and the corresponding difficulty, update the accuracy dictionary"""
    df = pd.read_csv(dir)

    # Create devs
    devs = []
    print(f"Evaluating CSV at {dir}")
    for q, a in tqdm(zip(df.question, df.AnswerKey)):
        if a not in ["A", "B", "C", "D"]:
            continue
        if len(devs) == 0: # First two questions with different answers as devs
            devs.append([q,a])
            continue
        if len(devs) == 1:
            if a != devs[0][1]:
                devs.append([q,a])
            continue

        accuracy[difficulty]["correct"] += isCorrect(q, devs, a)
        accuracy[difficulty]["total"] += 1


print(f"Now evaluating {config_kernel[kernel_config]} kernel")
# Evaluate all files
for difficulty, directories in question_dirs.items():
    for dir in directories:
        update_accuracy(dir, difficulty)
print("----------------------------")
print(f"There are {accuracy['easy']['total']} easy questions in total.\n\
       The GPT-2 model correctly answered {accuracy['easy']['correct']} questions.\n\
       The accuracy of ARC-Easy is {accuracy['easy']['correct']/accuracy['easy']['total']}")
print("----------------------------")
print(f"There are {accuracy['challenge']['total']} challenge questions in total.\n\
       The GPT-2 model correctly answered {accuracy['challenge']['correct']} questions.\n\
       The accuracy of ARC-Challenge is {accuracy['challenge']['correct']/accuracy['challenge']['total']}")
