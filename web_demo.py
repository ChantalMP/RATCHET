import tqdm
import datetime

import matplotlib.pyplot as plt
import numpy as np
import streamlit as st
import tensorflow as tf

from skimage import io
from model.transformer import Transformer
from model.utils import create_target_masks
from tokenizers import ByteLevelBPETokenizer


@st.cache(allow_output_mutation=True)
def load_validator():
    validator_model = tf.keras.models.load_model('checkpoints/cxr_validator_model.h5')
    print('Validator Model Loaded!')
    return validator_model


@st.cache(allow_output_mutation=True)
def load_model():

    # Load Tokenizer
    tokenizer = ByteLevelBPETokenizer(
        'preprocessing/mimic/mimic-vocab.json',
        'preprocessing/mimic/mimic-merges.txt',
    )

    # Model Hyperparameters
    target_vocab_size = tokenizer.get_vocab_size()
    dropout_rate = 0.1
    num_layers = 6
    d_model = 512
    dff = 2048
    num_heads = 8

    # Define RATCHET Transformer model
    transformer = Transformer(num_layers, d_model, num_heads, dff,
                              target_vocab_size=target_vocab_size,
                              rate=dropout_rate)

    # Restore RATCHET checkpoint
    checkpoint_path = "checkpoints/train0"
    ckpt = tf.train.Checkpoint(transformer=transformer)
    latest_checkpoint = tf.train.latest_checkpoint(checkpoint_path)
    ckpt.restore(latest_checkpoint)
    print(f'Model Loaded! Checkpoint file: {latest_checkpoint}')

    return transformer, tokenizer


def top_k_logits(logits, k):
    if k == 0:
        # no truncation
        return logits

    def _top_k():
        values, _ = tf.nn.top_k(logits, k=k)
        min_values = values[:, -1, tf.newaxis]
        return tf.where(
            logits < min_values,
            tf.ones_like(logits, dtype=logits.dtype) * -1e10,
            logits,
        )
    return tf.cond(
       tf.equal(k, 0),
       lambda: logits,
       lambda: _top_k(),
    )


def top_p_logits(logits, p):
    """Nucleus sampling"""
    batch, _ = logits.shape.as_list()
    sorted_logits = tf.sort(logits, direction='DESCENDING', axis=-1)
    cumulative_probs = tf.cumsum(tf.nn.softmax(sorted_logits, axis=-1), axis=-1)
    indices = tf.stack([
        tf.range(0, batch),
        # number of indices to include
        tf.maximum(tf.reduce_sum(tf.cast(cumulative_probs <= p, tf.int32), axis=-1) - 1, 0),
    ], axis=-1)
    min_values = tf.gather_nd(sorted_logits, indices)
    return tf.where(
        logits < min_values,
        tf.ones_like(logits) * -1e10,
        logits,
    )


def evaluate(inp_img, tokenizer, transformer, temperature, top_k, top_p, options, seed, MAX_LENGTH=128):

    # The first token to the transformer should be the start token
    output = tf.convert_to_tensor([[tokenizer.token_to_id('<s>')]])

    my_bar = st.progress(0)
    for i in tqdm.tqdm(range(MAX_LENGTH)):
        my_bar.progress(i/MAX_LENGTH)
        combined_mask = create_target_masks(output)

        # predictions.shape == (batch_size, seq_len, vocab_size)
        predictions, attention_weights = transformer(inp_img,
                                                     output,
                                                     False,
                                                     combined_mask,
                                                     None)

        # select the last word from the seq_len dimension
        predictions = predictions[:, -1, :] / temperature  # (batch_size, vocab_size)
        predictions = top_k_logits(predictions, k=top_k)
        predictions = top_p_logits(predictions, p=top_p)

        if options == 'Greedy':
            predicted_id = tf.cast(tf.argmax(predictions, axis=-1), tf.int32)[:, tf.newaxis]
        elif options == 'Sampling':
            predicted_id = tf.random.categorical(predictions, num_samples=1, dtype=tf.int32, seed=seed)
        else:
            st.write('SHOULD NOT HAPPEN')

        # return the result if the predicted_id is equal to the end token
        if predicted_id == 2:  # stop token #tokenizer_en.vocab_size + 1:
            my_bar.empty()
            return tf.squeeze(output, axis=0)[1:], attention_weights, i

        # concatentate the predicted_id to the output which is given to the decoder
        # as its input.
        output = tf.concat([output, predicted_id], axis=-1)

    my_bar.empty()

    return tf.squeeze(output, axis=0)[1:], attention_weights, i


def main():

    st.title('Chest X-ray AI Diagnosis Demo')
    st.text('Made with Streamlit and Attention RNN')

    transformer, tokenizer = load_model()
    cxr_validator_model = load_validator()

    st.sidebar.title('Configuration')
    options = st.sidebar.selectbox('Generation Method', ('Greedy', 'Sampling'))
    seed = st.sidebar.number_input('Sampling Seed:', value=42)
    temperature = st.sidebar.slider('Temperature', min_value=0., max_value=1., value=1., step=0.01)
    top_k = st.sidebar.slider('top_k', min_value=0, max_value=tokenizer.get_vocab_size(), value=0, step=1)
    top_p = st.sidebar.slider('top_p', min_value=0., max_value=1., value=1., step=0.01)

    st.set_option('deprecation.showfileUploaderEncoding', False)
    uploaded_file = st.file_uploader('Choose an image...', type=('png', 'jpg', 'jpeg'))

    if uploaded_file:

        # Read input image with size [1, H, W, 1] and range (0, 255)
        img_array = io.imread(uploaded_file, as_gray=True)[None, ..., None]

        # Convert image to float values in (0, 1)
        img_array = tf.image.convert_image_dtype(img_array, tf.float32)

        # Resize image with padding to [1, 224, 224, 1]
        img_array = tf.image.resize_with_pad(img_array, 224, 224, method=tf.image.ResizeMethod.BILINEAR)

        # Display input image
        st.image(np.squeeze(img_array.numpy()), caption='Uploaded Image')

        # Check image
        valid = tf.nn.sigmoid(cxr_validator_model(img_array))
        if valid < 0.1:
            st.info('Image is not a Chest X-ray')
            return

        # Log datetime
        print('[{}] Running Analysis...'
              .format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        # Generate radiology report
        with st.spinner('Generating report... Do not refresh or close window.'):
            result, attention_weights, tokens = evaluate(img_array, tokenizer, transformer,
                                                         temperature, top_k, top_p,
                                                         options, seed)
            predicted_sentence = tokenizer.decode(result)

        # Display generated text
        st.subheader('Generated Report:')
        st.write(predicted_sentence)
        # st.info(predicted_sentence)

        st.subheader('Attention Plot:')

        attn_map = attention_weights['decoder_layer6_block2'][0]
        attn_map = tf.reduce_mean(attn_map, axis=0)
        attn_map = attn_map / attn_map.numpy().max() * 255

        fig = plt.figure(figsize=(40, 80))

        for i in range(tokens - 1):
            attn_token = attn_map[i, ...]
            attn_token = tf.reshape(attn_token, [7, 7])

            ax = fig.add_subplot(16, 8, i + 1)
            ax.set_title(tokenizer.decode([result.numpy()[i]]))
            img = ax.imshow(np.squeeze(img_array))
            ax.imshow(attn_token, cmap='gray', alpha=0.6, extent=img.get_extent())

        st.pyplot(plt)

        # Run again?
        st.button('Regenerate Report')


if __name__ == '__main__':

    tf.config.set_visible_devices([], 'GPU')

    main()
