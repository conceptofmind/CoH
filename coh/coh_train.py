import dataclasses
import pprint
import re
from functools import partial

import absl.app
import absl.flags
import flax
import jax
import jax.numpy as jnp
import numpy as np
import optax
from flax import linen as nn
from flax.jax_utils import prefetch_to_device
from flax.training.train_state import TrainState
from jax.experimental import PartitionSpec as PS
from jax.experimental.pjit import pjit, with_sharding_constraint
from tqdm import tqdm, trange

from coh.data import HumanFeedbackDataset, PretrainDataset
from coh.jax_utils import (JaxRNG, ShardingHelper, StreamingCheckpointer,
                          cross_entropy_loss_and_accuracy, get_jax_mp_mesh,
                          global_norm, match_partition_rules, named_tree_map,
                          next_rng, set_random_seed, get_metrics, OptimizerFactory)
from coh.utils import (WandBLogger, define_flags_with_default, get_user_flags,
                      load_pickle, user_flags_to_config_dict)
from coh.models.gptj.gptj import FlaxGPTJForCausalLMModule, GPTJConfig
from coh.models.opt.opt import FlaxOPTForCausalLMModule, OPTConfig


FLAGS_DEF = define_flags_with_default(
    seed=42,
    initialize_jax_distributed=False,
    mp_mesh_dim=1,
    total_steps=10000,
    load_checkpoint='',
    load_dataset_state='',
    log_freq=50,
    save_model_freq=0,
    optimizer=OptimizerFactory.get_default_config(),
    tokenizer=GPTJConfig.get_tokenizer_config(),
    feedback_dataset=HumanFeedbackDataset.get_default_config(),
    pretrain_dataset=PretrainDataset.get_default_config(),
    pt_loss_weight=1.0,
    gptj=GPTJConfig.get_default_config(),
    load_gptj_config='',
    opt=OPTConfig.get_default_config(),
    load_opt_config='',
    model='gptj',
    logger=WandBLogger.get_default_config(),
    log_all_worker=False,
)


def main(argv):
    FLAGS = absl.app.flags.FLAGS
    if FLAGS.initialize_jax_distributed:
        jax.distributed.initialize()

    variant = get_user_flags(FLAGS, FLAGS_DEF)
    flags_config_dict = user_flags_to_config_dict(FLAGS, FLAGS_DEF)
    logger = WandBLogger(
        config=FLAGS.logger,
        variant=variant,
        enable=FLAGS.log_all_worker or (jax.process_index() == 0),
    )
    set_random_seed(FLAGS.seed)

    if FLAGS.load_dataset_state != '':
        hf, pt = FLAGS.load_dataset_state.split(',')
        hf_dataset = load_pickle(hf)
        pt_dataset = load_pickle(pt)
    else:
        tokenizer = GPTJConfig.get_tokenizer(FLAGS.tokenizer)
        hf_dataset = HumanFeedbackDataset(FLAGS.feedback_dataset, tokenizer)
        pt_dataset = PretrainDataset(FLAGS.pretrain_dataset, tokenizer)

    seq_length = hf_dataset.seq_length

    if FLAGS.model == 'gptj':
        if FLAGS.load_gptj_config != '':
            gptj_config = GPTJConfig.load_config(FLAGS.load_gptj_config)
        else:
            gptj_config = GPTJConfig(**FLAGS.gptj)
        gptj_config.update(dict(
            bos_token_id=hf_dataset.tokenizer.bos_token_id,
            eos_token_id=hf_dataset.tokenizer.eos_token_id,
            vocab_size=hf_dataset.vocab_size,
        ))
        model = FlaxGPTJForCausalLMModule(gptj_config)
        config = gptj_config
    elif FLAGS.model == 'opt':
        if FLAGS.load_opt_config != '':
            opt_config = OPTConfig.load_config(FLAGS.load_opt_config)
        else:
            opt_config = OPTConfig(**FLAGS.opt)

        opt_config.update(dict(
            bos_token_id=hf_dataset.tokenizer.bos_token_id,
            eos_token_id=hf_dataset.tokenizer.eos_token_id,
        ))
        if opt_config.vocab_size < hf_dataset.vocab_size:
            opt_config.update(dict(vocab_size=hf_dataset.vocab_size))
        model = FlaxOPTForCausalLMModule(opt_config)
        config = opt_config
    else:
        raise ValueError(f'Unknown model: {FLAGS.model}')

    def weight_decay_mask(params):
        def decay(name, _):
            for rule in GPTJConfig.get_weight_decay_exclusions():
                if re.search(rule, name) is not None:
                    return False
            return True
        return named_tree_map(decay, params, sep='/')

    optimizer, optimizer_info = OptimizerFactory.get_optimizer(
        FLAGS.optimizer, weight_decay_mask
    )

    def init_fn(rng):
        rng_generator = JaxRNG(rng)
        params = model.init(
            input_ids=jnp.zeros((4, seq_length), dtype=jnp.int32),
            position_ids=jnp.zeros((4, seq_length), dtype=jnp.int32),
            attention_mask=jnp.ones((4, seq_length), dtype=jnp.int32),
            rngs=rng_generator(config.rng_keys()),
        )
        return TrainState.create(params=params, tx=optimizer, apply_fn=None)

    def train_step(train_state, rng, batch, pt_batch):
        rng_generator = JaxRNG(rng)
        tokens = with_sharding_constraint(batch['tokens'], PS('dp'))
        pt_tokens = with_sharding_constraint(pt_batch['tokens'], PS('dp'))
        loss_masks = with_sharding_constraint(batch['masks'], PS('dp'))
        def loss_and_accuracy(params):
            bos_tokens = jnp.full(
                (tokens.shape[0], 1), config.bos_token_id, dtype=jnp.int32
            )
            # human feedback data
            inputs = jnp.concatenate([bos_tokens, tokens[:, :-1]], axis=1)
            logits = model.apply(
                params, inputs, deterministic=False,
                rngs=rng_generator(config.rng_keys()),
            ).logits
            hf_loss, hf_accuracy = cross_entropy_loss_and_accuracy(logits, tokens, loss_masks)
            # general pretrain data
            bos_tokens = jnp.full(
                (pt_tokens.shape[0], 1), config.bos_token_id, dtype=jnp.int32
            )
            pt_inputs = jnp.concatenate([bos_tokens, pt_tokens[:, :-1]], axis=1)
            pt_logits = model.apply(
                params, pt_inputs, deterministic=False,
                rngs=rng_generator(config.rng_keys()),
            ).logits
            pt_loss, pt_accuracy = cross_entropy_loss_and_accuracy(pt_logits, pt_tokens)
            loss = hf_loss + FLAGS.pt_loss_weight * pt_loss
            aux = {
                'hf_accuracy': hf_accuracy,
                'pt_accuracy': pt_accuracy,
                'hf_loss': hf_loss,
                'pt_loss': pt_loss,
            }
            return loss, aux
        grad_fn = jax.value_and_grad(loss_and_accuracy, has_aux=True)
        (loss, aux), grads = grad_fn(train_state.params)
        train_state = train_state.apply_gradients(grads=grads)
        metrics = dict(
            loss=loss,
            learning_rate=optimizer_info['learning_rate_schedule'](train_state.step),
            gradient_norm=global_norm(grads),
            param_norm=global_norm(train_state.params),
        )
        metrics.update(aux)
        return train_state, rng_generator(), metrics

    train_state_shapes = jax.eval_shape(init_fn, next_rng())
    train_state_partition = match_partition_rules(
        GPTJConfig.get_partition_rules(), train_state_shapes
    )

    sharding_helper = ShardingHelper(train_state_partition)
    checkpointer = StreamingCheckpointer(
        logger.checkpoint_dir, enable=jax.process_index() == 0
    )

    sharded_init_fn = pjit(
        init_fn,
        in_axis_resources=PS(),
        out_axis_resources=train_state_partition
    )

    sharded_train_step = pjit(
        train_step,
        in_axis_resources=(train_state_partition, PS(), PS(), PS()),
        out_axis_resources=(train_state_partition, PS(), PS()),
        donate_argnums=(0, 1),
    )

    def save_checkpoint(train_state):
        train_state = sharding_helper.get(train_state)
        step = int(train_state.step)
        metadata = dict(
            step=step,
            variant=variant,
            flags=flags_config_dict,
            config=config.to_dict(),
        )
        checkpointer.save_pickle(metadata, 'metadata.pkl')
        checkpointer.save_pickle(hf_dataset, 'hf_dataset.pkl')
        checkpointer.save_pickle(pt_dataset, 'pt_dataset.pkl')
        checkpointer.save_checkpoint(train_state, 'train_state')

    start_step = 0
    restored_checkpoint_state = None
    restored_params = None
    if FLAGS.load_checkpoint != '':
        load_type, load_path = FLAGS.load_checkpoint.split('::', 1)
        with jax.default_device(jax.devices("cpu")[0]):
            if load_type == 'trainstate':
                restored_checkpoint_state = checkpointer.load_checkpoint(
                    load_path, train_state_shapes
                )
                start_step = restored_checkpoint_state.step
            elif load_type == 'trainstate_params':
                restored_params = flax.core.frozen_dict.freeze(
                    checkpointer.load_checkpoint(load_path)['params']
                )
            elif load_type == 'huggingface':
                restored_params = config.load_pretrained(load_path)

    mesh = get_jax_mp_mesh(FLAGS.mp_mesh_dim)
    with mesh:
        if restored_checkpoint_state is not None:
            train_state = sharding_helper.put(restored_checkpoint_state)
            del restored_checkpoint_state
        elif restored_params is not None:
            train_state = sharded_init_fn(next_rng())
            train_state = sharding_helper.get(train_state)
            train_state = train_state.replace(params=restored_params)
            train_state = sharding_helper.put(train_state)
            del restored_params
        else:
            train_state = sharded_init_fn(next_rng())

        if FLAGS.save_model_freq > 0:
            save_checkpoint(train_state)

        sharded_rng = next_rng()

        step_counter = trange(start_step, FLAGS.total_steps, ncols=0)

        for step, hf_batch, pt_batch in zip(step_counter, hf_dataset, pt_dataset):
            train_state, sharded_rng, metrics = sharded_train_step(
                train_state, sharded_rng, hf_batch, pt_batch
            )

            if step % FLAGS.log_freq == 0:
                log_metrics = {"step": step}
                log_metrics.update(metrics)
                logger.log(log_metrics)
                tqdm.write("\n" + pprint.pformat(log_metrics) + "\n")

            if FLAGS.save_model_freq > 0 and (step + 1) % FLAGS.save_model_freq == 0:
                save_checkpoint(train_state)

        if FLAGS.save_model_freq > 0:
            save_checkpoint(train_state)


if __name__ == "__main__":
    absl.app.run(main)
