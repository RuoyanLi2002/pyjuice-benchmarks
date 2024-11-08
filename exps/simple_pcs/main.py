import argparse
import torch
import torchvision
import sys
import os
import time
import uuid
import pyjuice as juice

from omegaconf import OmegaConf

sys.path.append("../../")

from src.utils import instantiate_from_config, collect_data_from_dsets, ProgressBar

# python main.py --batch-size 128 --data-config ../../configs/data/imagenet32.yaml --model-config ../../configs/model/pd_64.yaml
# python main.py --batch-size 128 --data-config ../../configs/data/imagenet32_gaussian.yaml --model-config ../../configs/model/pd_64.yaml
def resolve_tuple(*args):
    return tuple(args)

noise_type = 'gaussian'
print(f"noise_type: {noise_type}")

def add_noise(x, noise_type='uniform', noise_level=0.01):
    if noise_type == 'uniform':
        noise = torch.rand_like(x) * 2 * noise_level - noise_level
    elif noise_type == 'gaussian':
        noise = torch.randn_like(x) * noise_level
    
    return x + noise


def mini_batch_em_epoch(num_epochs, pc, optimizer, scheduler, train_loader, val_loader, device):
    print("Mini Batch EM")
    if num_epochs == 0:
        return -1000000.0, -1000000.0, [(-1000000.0, -1000000.0)]

    best_ll_pair = (-1000000.0, -1000000.0)
    all_ll_pairs = []

    progressbar = ProgressBar(num_epochs, len(train_loader), statistics_name = ["LL"], cumulate_statistics = True)
    for epoch in range(num_epochs):
        progressbar.new_epoch_begin()
        pc.init_param_flows(flows_memory = 0.0) # Zero out flows
        total_train_ll = 0.0
        for x in train_loader:
            x = x.to(device, non_blocking = True)
            print(x)
            print(x.shape)
            bs = x.shape[0]
            x = x.reshape(bs, 3, 16, 16)
            print(x.shape)
            x = x[:, 0, :, :]
            print(x.shape)
            x = x.reshape(bs, -1)
            print(x.shape)
            
            exit()
            optimizer.zero_grad()

            lls = pc(x)
            lls.mean().backward()

            progressbar.new_batch_done([lls.mean().detach().cpu().numpy().item()])
            total_train_ll += lls.mean().detach().cpu().numpy().item()

            optimizer.step()
            if scheduler is not None:
                scheduler.step()

        aveg_train_ll = total_train_ll / len(train_loader)
        progressbar.epoch_ends()

        total_val_ll = 0.0
        with torch.no_grad():
            for x in val_loader:
                x = x.to(device)

                lls = pc(x)
                total_val_ll += lls.mean().detach().cpu().numpy().item()

        aveg_val_ll = total_val_ll / len(val_loader)
        print(f"Validation LL: {total_val_ll / len(val_loader)}")

        if aveg_val_ll > best_ll_pair[1]:
            best_ll_pair = (aveg_train_ll, aveg_val_ll)

        all_ll_pairs.append((aveg_train_ll, aveg_val_ll))

    return best_ll_pair[0], best_ll_pair[1], all_ll_pairs


def full_batch_em_epoch(num_epochs, pc, ns, train_loader, val_loader, device):
    print("Full Batch EM")
    if num_epochs == 0:
        return -1000000.0, -1000000.0, [(-1000000.0, -1000000.0)]

    best_ll_pair = (-1000000.0, -1000000.0)
    all_ll_pairs = []

    for epoch in range(num_epochs):
        with torch.no_grad():
            t0 = time.time()
            total_train_ll = 0.0
            for x in train_loader:
                # pc.init_param_flows(flows_memory = 0.0) # Zero out flows
                x = x.to(device, non_blocking = True)
                
                x = x / 127.5 - 1
                x = add_noise(x, noise_type='uniform', noise_level=0.01)
                bs = x.shape[0]
                x = x.reshape(bs, 3, 16, 16)
                x = x[:, 0, :, :]
                x = x.reshape(bs, -1)

                # exit()
                lls = pc(x)

                pc.backward(x, flows_memory = 1.0, logspace_flows = False, allow_modify_flows = False)
                total_train_ll += lls.mean().detach().cpu().numpy().item()

            pc.mini_batch_em(step_size = 1.0, pseudocount = 0.1)

            aveg_train_ll = total_train_ll / len(train_loader)

            total_val_ll = 0.0
            with torch.no_grad():
                for x in val_loader:
                    x = x.to(device)

                    x = x / 127.5 - 1
                    x = add_noise(x, noise_type='uniform', noise_level=0.01)
                    bs = x.shape[0]
                    x = x.reshape(bs, 3, 16, 16)
                    x = x[:, 0, :, :]
                    x = x.reshape(bs, -1)

                    lls = pc(x)
                    total_val_ll += lls.mean().detach().cpu().numpy().item()

            aveg_val_ll = total_val_ll / len(val_loader)

            t1 = time.time()

            print(f"[train LL: {aveg_train_ll:.2f}; val LL: {aveg_val_ll:.2f}].....[time {t1-t0:.2f}] ")

        if aveg_val_ll > best_ll_pair[1]:
            best_ll_pair = (aveg_train_ll, aveg_val_ll)

            pc.update_parameters()
            juice.save(fname = f"model_gaussian.jpc", model = ns)

        all_ll_pairs.append((aveg_train_ll, aveg_val_ll))

    return best_ll_pair[0], best_ll_pair[1], all_ll_pairs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-config", type = str, default = "../../configs/data/imagenet.yaml")
    parser.add_argument("--model-config", type = str, default = "../../configs/model/hclt_256.yaml")
    parser.add_argument("--optim-config", type = str, default = "../../configs/optim/full_batch_only.yaml")
    parser.add_argument("--batch-size", type = int, default = 0)
    parser.add_argument("--gpu", type = int, default = 0)

    args = parser.parse_args()

    # Device
    device = torch.device(f"cuda:{args.gpu}")
    
    # Dataset
    data_config = OmegaConf.load(args.data_config)
    if args.batch_size > 0:
        data_config["params"]["batch_size"] = args.batch_size
    dsets = instantiate_from_config(data_config)

    dsets.prepare_data()
    dsets.setup()
    print("> Dataset prepared")

    # Model
    model_config = OmegaConf.load(args.model_config)
    model_kwargs = {}
    for k, v in model_config["params"].items():
        if isinstance(v, str) and v.startswith("__train_data__:"):
            num_samples = int(v.split(":")[1])
            data = collect_data_from_dsets(dsets, num_samples = num_samples, split = "train")
            model_config["params"].pop(k, None)
            model_kwargs[k] = data.cuda()
    print("> Constructing PC...")
    print(f"model_config: {model_config}")
    ns = instantiate_from_config(model_config, **model_kwargs)
    pc = juice.TensorCircuit(ns)
    pc.print_statistics()
    

    pc.to(device)


    # Optimizer and scheduler
    optim_config = OmegaConf.load(args.optim_config)
    mini_batch_num_epochs = optim_config["mini_batch_num_epochs"]
    full_batch_num_epochs = optim_config["full_batch_num_epochs"]
    optimizer = juice.optim.CircuitOptimizer(pc, **optim_config["optim_params"])
    scheduler = juice.optim.CircuitScheduler(optimizer, **optim_config["scheduler_params"])

    train_loader = dsets._train_dataloader()
    val_loader = dsets._val_dataloader()

    # best_tr_ll1, best_vl_ll1, ll_pairs1 = mini_batch_em_epoch(
    #     mini_batch_num_epochs, pc, optimizer, scheduler, train_loader, val_loader, device
    # )
    best_tr_ll2, best_vl_ll2, ll_pairs2 = full_batch_em_epoch(
        full_batch_num_epochs, pc, ns, train_loader, val_loader, device
    )

    if best_vl_ll1 > best_vl_ll2:
        best_tr_ll = best_tr_ll1
        best_vl_ll = best_vl_ll1
    else:
        best_tr_ll = best_tr_ll2
        best_vl_ll = best_vl_ll2

    ll_pairs = ll_pairs1 + ll_pairs2

    # Logging
    # fname = os.path.join("logs/", f"{str(uuid.uuid4())}.txt")
    # with open(fname, "w") as f:
    #     f.write(f"Data config: {args.data_config}\n")
    #     f.write(f"Model config: {args.model_config}\n")
    #     f.write(f"Optim config: {args.optim_config}\n")

    #     f.write(f"Best lls: ({best_tr_ll}, {best_vl_ll})\n\n")

    #     for epoch, (tr_ll, vl_ll) in enumerate(ll_pairs):
    #         f.write(f"Epoch {epoch+1} - train LL: {tr_ll:.2f} - validation LL: {vl_ll:.2f}\n")

    juice.save(fname = f"model_gaussian.jpc", model = ns)


if __name__ == "__main__":
    torchvision.disable_beta_transforms_warning()
    OmegaConf.register_new_resolver('as_tuple', resolve_tuple)
    main()
