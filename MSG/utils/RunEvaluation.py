import torch
import torch.nn.functional as F

import nussl
import librosa
import yaml
import os
from collections import namedtuple
from model_factory import ModelFactory
import datasets.EvaluationDataset as EV
import numpy as np
import soundfile as sf
import json

class Struct:
     def __init__(self, **entries):
         for key, value in entries.items():
             if isinstance(value, dict):
                 self.__dict__.update({key:Struct(**value)})
             else:
                self.__dict__.update({key:value})

def parseConfig(config):
    exp_dict = yaml.load(open(os.path.join(config), 'r'),
                         Loader=yaml.FullLoader)
    return Struct(**exp_dict).parameters

def run_inference(netG, ds, start, end, shift, reduction_factor, device):
    song_length = end - start
    generated = np.zeros((song_length + shift) * reduction_factor)
    ground_truth = np.zeros((song_length + shift) * reduction_factor)
    noisy = np.zeros((song_length + shift) * reduction_factor)
    mix = np.zeros((song_length + shift) * reduction_factor)
    for i in range(start, end + 1, 1):
        # second is to perform SDR, SIR, SAR evaluation on each song
        source_class, _ = ds[i]
        noisy_source = torch.from_numpy(source_class[1]).unsqueeze(0).unsqueeze(0).to(device)
        ground_truth_source = source_class[2]
        mixture_segment = source_class[3]

        # perform the inference
        inp = F.pad(noisy_source, (4000, 4000), "constant", 0)
        x_pred_t = netG(inp, noisy_source.unsqueeze(1)).squeeze(1)
        a = x_pred_t.squeeze().squeeze().detach().cpu().numpy()

        # get the offsets and current segment index
        offset = start
        ind = i - offset

        # Add overlapping signals together
        generated[ind * reduction_factor: (ind + shift) * reduction_factor] += a
        noisy[ind * reduction_factor: (ind + shift) * reduction_factor] += \
            noisy_source.cpu().squeeze(0).squeeze(0).numpy()
        ground_truth[ind * reduction_factor: (ind + shift) * reduction_factor] += \
            ground_truth_source
        mix[ind * reduction_factor: (ind + shift) * reduction_factor] += \
            mixture_segment

    # handle edge cases of the overlap add
    for i in range(0, shift - 1):
        generated[i * reduction_factor:(i + 1) * reduction_factor] /= (i + 1)
        generated[-(i + 1) * reduction_factor:-i * reduction_factor] /= (i + 1)

        ground_truth[i * reduction_factor:(i + 1) * reduction_factor] /= (i + 1)
        ground_truth[-(i + 1) * reduction_factor:-i * reduction_factor] /= (i + 1)

        noisy[i * reduction_factor:(i + 1) * reduction_factor] /= (i + 1)
        noisy[-(i + 1) * reduction_factor:-i * reduction_factor] /= (i + 1)

        mix[i * reduction_factor:(i + 1) * reduction_factor] /= (i + 1)
        mix[-(i + 1) * reduction_factor:-i * reduction_factor] /= (i + 1)

    # handle average case of the overlap add
    generated[(i + 1) * reduction_factor:-(i + 1) * reduction_factor] /= (i + 2)
    noisy[(i + 1) * reduction_factor:-(i + 1) * reduction_factor] /= (i + 2)
    ground_truth[(i + 1) * reduction_factor:-(i + 1) * reduction_factor] /= (i + 2)
    mix[(i + 1) * reduction_factor:-(i + 1) * reduction_factor] /= (i + 2)

    # remove padding introduced during inference
    noisy = noisy[0:(-i + 2) * reduction_factor]
    generated = generated[0:(-i + 2) * reduction_factor]
    ground_truth = ground_truth[0:(-i + 2) * reduction_factor]
    mix = mix[0:(-i + 2) * reduction_factor]
    return noisy, ground_truth, mix, generated

def run_single_evaluation(ground_truth, remaining_ground_truth, target_source, remaining_target_source):
    bss_eval = nussl.evaluation.BSSEvalV4(
        true_sources_list=[ground_truth, remaining_ground_truth],
        estimated_sources_list=[target_source, remaining_target_source],
    )
    noisy_eval = bss_eval.evaluate()
    return np.nanmedian(noisy_eval['source_0']['SDR'])

def convert_to_audio(noisy, ground_truth, generated):
    ground_truth_signal = nussl.AudioSignal(audio_data_array=ground_truth)
    noisy_signal = nussl.AudioSignal(audio_data_array=noisy)
    generated_signal = nussl.AudioSignal(audio_data_array=generated)
    return ground_truth_signal, noisy_signal, generated_signal


def Evaluate(config,names):
    config = parseConfig(config)
    best_g = config.best_g
    results = {}
    for i in range(len(config.test_sources_paths)):
        baseline,msg = EvaluateLoop(config,best_g,names,config.test_sources_paths[i],config.evaluation_models[i])
        results[f'{config.test_sources_paths[i]} SDR'] = baseline
        results[f'{config.test_sources_paths[i]} + MSG SDR'] = msg
    with open(f'/home/noah/{config.source}_sdr_results.json', 'w') as f:
        json.dump(results, f)

def EvaluateLoop(config, best_g, names,dataset_path,dataset_name) -> tuple:
    
    # get device
    device = torch.device(f"cuda:0" if torch.cuda.is_available() else "cpu")

    # load the config as an object
    

    # load the generator, load the checkpoint, send to device
    model_selector = ModelFactory(config, torch.optim.Adam)
    netG = model_selector.generator().to(device)
    measurements = []

    # create the dataset
    # no need for a wrapper because we are iterating over single items in the
    # set (no batches)
    eval_set = EV.EvalSet(dataset_path=dataset_path,
                          item_length=config.segment_duration,
                          sample_rate=config.sample_rate,
                          sources=(f'dirty_{config.source}',config.source),
                          as_dict=False,
                          hop_length=config.hop_len)

    # list of start indices and end indices
    song_indices = eval_set.get_song_indices()
    shift = int(1 / config.hop_len)
    reduction_factor = int(config.sample_rate * config.hop_len)

    first_iter = True

    for i in range(len(best_g)):
        netG.load_state_dict(
            torch.load(best_g[i]))

        
        # run evaluation on each song:
        netG.eval()

        measurements_baseline = []
        measurements_msg = []
        with torch.no_grad():
            for iterno, (start, end) in enumerate(song_indices):
                print(names[i],':', eval_set[start][1])
                noisy, ground_truth, mix, generated = run_inference(netG, eval_set,
                                                                    start, end,
                                                                    shift,
                                                                    reduction_factor,
                                                                    device)
                ground_truth_signal, noisy_signal, generated_signal = convert_to_audio(noisy, ground_truth, generated)
                if eval_set[start][1] in config.song_names:
                    if first_iter:
                        sf.write(f'Ground_Truth_{dataset_name}_{iterno}.wav', ground_truth_signal.peak_normalize().audio_data.T, config.sample_rate)
                        sf.write(f'generated_{dataset_name}_{iterno}.wav', noisy_signal.peak_normalize().audio_data.T, config.sample_rate)
                    sf.write(f'generated_{dataset_name}_{names[i]}_{iterno}.wav', generated_signal.peak_normalize().audio_data.T, config.sample_rate)
                if first_iter:
                    measurements_baseline.append(run_single_evaluation(ground_truth_signal, gt_remaining_sources, noisy_signal, noisy_remaining_sources))
                measurements_msg.append(run_single_evaluation(ground_truth_signal, gt_remaining_sources, generated_signal, generated_remaining_sources))
            first_iter = False

    return np.nanmedian(measurements_baseline),np.nanmedian(measurements_msg)
