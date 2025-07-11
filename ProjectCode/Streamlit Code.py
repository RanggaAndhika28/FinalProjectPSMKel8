import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from PIL import Image
import numpy as np
import os
import zipfile
import tempfile
import glob
import shutil
import io
import re
from contextlib import redirect_stdout
import atexit

# --- TensorFlow and Keras Imports ---
import tensorflow as tf
from tensorflow.keras.models import Model
from tensorflow.keras.layers import Dense, Dropout, GlobalAveragePooling2D
from tensorflow.keras.applications import MobileNetV2
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.callbacks import EarlyStopping, ReduceLROnPlateau
from sklearn.model_selection import train_test_split
# --- NEW IMPORTS for final evaluation ---
from sklearn.metrics import classification_report, confusion_matrix, ConfusionMatrixDisplay

# --- 1. Page Configuration ---
st.set_page_config(
    page_title="Embryo Image Explorer",
    page_icon="🧬",
    layout="wide"
)

# --- Session State Initialization ---
if 'dataset_extracted' not in st.session_state:
    st.session_state.dataset_extracted = False
if 'temp_dir' not in st.session_state:
    st.session_state.temp_dir = None
if 'model_with_es' not in st.session_state:
    st.session_state.model_with_es = None
if 'model_without_es' not in st.session_state:
    st.session_state.model_without_es = None
if 'model_classes' not in st.session_state:
    st.session_state.model_classes = None
if 'history_with_es' not in st.session_state:
    st.session_state.history_with_es = None
if 'history_without_es' not in st.session_state:
    st.session_state.history_without_es = None


# --- 2. Title and Introduction ---
st.title("🧬 Embryo Image Dataset Explorer")
st.markdown(
    """
    This application allows you to explore an embryo image dataset, train a state-of-the-art CNN model,
    and **classify new embryo images**.

    The system automatically extracts day information from filenames (e.g., "D3_image.jpg") and uses a
    **combined weighting strategy** to handle imbalances in both classes and days during training.
    It will use a `train` folder for training/validation and a `test` folder for final model evaluation.
    """
)

# --- 3. Helper Functions ---

def extract_day_from_filename(filename):
    """Extract day number from a filename that starts with D followed by a number."""
    match = re.match(r'D(\d+)', os.path.basename(filename))
    if match:
        return int(match.group(1))
    return None

def extract_and_setup_persistent_directory(uploaded_file):
    """Extract zip file and find 'train' and 'test' directories."""
    if st.session_state.temp_dir is None:
        st.session_state.temp_dir = tempfile.mkdtemp()
    
    temp_dir = st.session_state.temp_dir
    
    try:
        with zipfile.ZipFile(uploaded_file, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
        
        found_train_path = None
        found_test_path = None
        for root, dirs, files in os.walk(temp_dir):
            if 'train' in dirs:
                found_train_path = os.path.join(root, 'train')
            if 'test' in dirs:
                found_test_path = os.path.join(root, 'test')
        
        if found_train_path is None:
            st.error("Could not find a 'train' directory inside the zip file. This folder is required.")
            return None, None
        
        return temp_dir, found_train_path, found_test_path
    
    except zipfile.BadZipFile:
        st.error("The uploaded file is not a valid ZIP file.")
        return None, None, None
    except Exception as e:
        st.error(f"An unexpected error occurred during data loading: {e}")
        return None, None, None

@st.cache_data(show_spinner="Processing folder: {folder_name}...")
def process_image_data(folder_path, _temp_dir, folder_name=""):
    """Process images, extract metadata, and create a DataFrame."""
    if not folder_path or not os.path.exists(folder_path):
        return pd.DataFrame(), [], [], 0, {}
        
    classes = sorted([d for d in os.listdir(folder_path) if os.path.isdir(os.path.join(folder_path, d))])
    data, corrupt_images = [], []
    image_count = 0
    day_extraction_stats = {'total_files': 0, 'with_day': 0, 'without_day': 0}
    
    for label in classes:
        class_path = os.path.join(folder_path, label)
        for img_path in glob.glob(os.path.join(class_path, '*.jpg')):
            day_extraction_stats['total_files'] += 1
            try:
                with Image.open(img_path) as img:
                    width, height = img.size
                    filename = os.path.basename(img_path)
                    day = extract_day_from_filename(filename)
                    
                    if day is not None:
                        day_extraction_stats['with_day'] += 1
                    else:
                        day_extraction_stats['without_day'] += 1
                    
                    data.append({
                        'filename': filename, 'class': label, 'day': day,
                        'width': width, 'height': height,
                        'aspect_ratio': width / height if height > 0 else 0,
                        'path': img_path
                    })
                    image_count += 1
            except Exception:
                corrupt_images.append(img_path)
    
    return pd.DataFrame(data), classes, corrupt_images, image_count, day_extraction_stats


def preprocess_image_for_prediction(image, target_size=(224, 224)):
    """Preprocess a single image for model prediction."""
    if image.mode != 'RGB':
        image = image.convert('RGB')
    image_resized = image.resize(target_size)
    img_array = np.array(image_resized)
    img_array = np.expand_dims(img_array, axis=0)
    # Apply the same preprocessing used during training
    preprocessed_img = tf.keras.applications.mobilenet_v2.preprocess_input(img_array)
    return preprocessed_img


def predict_embryo_class(model, image, classes):
    """Predict the class of an embryo image."""
    processed_image = preprocess_image_for_prediction(image)
    predictions = model.predict(processed_image)[0]
    predicted_class_idx = np.argmax(predictions)
    predicted_class = classes[predicted_class_idx]
    confidence = float(np.max(predictions))
    class_probabilities = {classes[i]: float(predictions[i]) for i in range(len(classes))}
    return predicted_class, confidence, class_probabilities

def cleanup_temp_directory():
    """Clean up temporary directory on app termination."""
    if st.session_state.temp_dir and os.path.exists(st.session_state.temp_dir):
        try:
            shutil.rmtree(st.session_state.temp_dir)
            for key in list(st.session_state.keys()):
                del st.session_state[key]
        except Exception as e:
            st.warning(f"Could not clean up temporary directory: {e}")

def get_best_model():
    """Return the best available trained model from session state."""
    if 'model_with_es' in st.session_state and st.session_state.model_with_es is not None:
        return st.session_state.model_with_es
    return None

atexit.register(cleanup_temp_directory)

# --- Sidebar ---
with st.sidebar:
    st.header("Dataset Management")
    if st.button("🗑️ Clear Dataset & Reset"):
        cleanup_temp_directory()
        st.rerun()
    
    if st.session_state.dataset_extracted:
        st.success("✅ Dataset loaded")
    
    best_model = get_best_model()
    if best_model is not None:
        st.success("🤖 Model trained and ready!")
        st.info(f"Classes: {', '.join(st.session_state.model_classes)}")

# --- Main Application Logic ---
uploaded_file = st.file_uploader("Upload your .zip dataset (must contain 'train' and optionally 'test' folders)", type="zip")

if uploaded_file is not None:
    if not st.session_state.dataset_extracted:
        temp_dir, train_path, test_path = extract_and_setup_persistent_directory(uploaded_file)
        if temp_dir and train_path:
            st.session_state.dataset_extracted = True
            st.session_state.train_path = train_path
            st.session_state.test_path = test_path 

    if st.session_state.dataset_extracted:
        train_path = st.session_state.train_path
        test_path = st.session_state.get('test_path')

        train_df, classes, train_corrupt, train_count, train_day_stats = process_image_data(train_path, st.session_state.temp_dir, folder_name="train")
        
        if test_path:
            test_df, _, test_corrupt, test_count, test_day_stats = process_image_data(test_path, st.session_state.temp_dir, folder_name="test")
            st.session_state.test_df_final = test_df
        else:
            test_count = 0
            st.session_state.test_df_final = None

        if train_df is not None and not train_df.empty:
            st.success(f"Processed {train_count} images from 'train' folder and {test_count} images from 'test' folder.")
            
            st.header("Explorations & Visualizations")
            tab_list = [
                "Class Distribution", "Day Analysis", "Dimension Scatter", 
                "Aspect Ratio", "Sample Images", "Model Training"
            ]
            
            if st.session_state.test_df_final is not None and get_best_model() is not None:
                tab_list.append("🏆 Final Evaluation")
            
            tab_list.append("Try it Yourself!")
            
            tabs = st.tabs(tab_list)
            data_viz_tabs = {name: tabs[i] for i, name in enumerate(tab_list)}

            with data_viz_tabs["Class Distribution"]:
                st.subheader("Image Distribution in the Training Set")
                fig, ax = plt.subplots(); sns.countplot(x='class', data=train_df, hue='class', order=classes, palette='viridis', legend=False, ax=ax); plt.xticks(rotation=45); st.pyplot(fig)

            with data_viz_tabs["Day Analysis"]:
                st.subheader("📅 Day-based Analysis of Training Set")
                df_with_days = train_df[train_df['day'].notna()].copy()
                if not df_with_days.empty:
                    df_with_days['day'] = df_with_days['day'].astype(int)
                    st.subheader("Distribution of Images by Day"); fig, ax = plt.subplots(figsize=(10, 5)); sns.countplot(data=df_with_days, x='day', ax=ax, palette='viridis', hue='day', legend=False); st.pyplot(fig)
                    st.subheader("Class Distribution by Day"); fig, ax = plt.subplots(figsize=(12, 6)); sns.countplot(data=df_with_days, x='day', hue='class', ax=ax, palette='Set2'); st.pyplot(fig)
                else: st.warning("No images with day information found.")

            with data_viz_tabs["Dimension Scatter"]:
                st.subheader("Image Dimension Scatter Plot"); fig, ax = plt.subplots()
                if 'day' in train_df.columns and train_df['day'].notna().any(): sns.scatterplot(data=train_df, x='width', y='height', hue='day', palette='viridis', legend='full', ax=ax)
                else: sns.scatterplot(x='width', y='height', hue='class', data=train_df, palette='Set2', ax=ax)
                st.pyplot(fig)

            with data_viz_tabs["Aspect Ratio"]:
                st.subheader("Aspect Ratio Distribution"); fig, ax = plt.subplots(); sns.histplot(data=train_df, x='aspect_ratio', hue='class', bins=30, palette='coolwarm', ax=ax, multiple="stack"); st.pyplot(fig)
            
            with data_viz_tabs["Sample Images"]:
                st.subheader("Sample Images from Training Set")
                if classes:
                    cols = st.columns(len(classes))
                    for i, label in enumerate(classes):
                        sample_df = train_df[train_df['class'] == label].sample(1)
                        if not sample_df.empty:
                            sample_row = sample_df.iloc[0]; img_path = sample_row['path']
                            caption = f"Class: {label}"
                            if pd.notna(sample_row.get('day')): caption += f", Day: {int(sample_row['day'])}"
                            cols[i].image(Image.open(img_path), caption=caption, use_container_width=True)

            # --- MODEL TRAINING TAB ---
            with data_viz_tabs["Model Training"]:
                st.header("🧠 Model Training & Evaluation")
                st.markdown("""
                    This tab runs a comparison between two training approaches, both using a **combined weighting strategy** to handle data imbalance for classes and days.
                    1.  **With Early Stopping & Fine-Tuning**: Trains the classifier, stops when validation performance plateaus, then unfreezes and fine-tunes deeper layers for better accuracy.
                    2.  **Without Early Stopping**: Training runs for the full number of epochs specified.
                """)
                def create_dataset(dataframe, class_indices, day_weights_dict, class_weights_dict, batch_size, image_size, is_training=True):
                    df_copy = dataframe.copy(); df_copy['day'] = df_copy['day'].fillna(-1).astype(int); df_copy['label'] = df_copy['class'].map(class_indices)
                    day_weight = df_copy['day'].map(day_weights_dict).fillna(1.0); class_weight = df_copy['class'].map(class_weights_dict); df_copy['sample_weight'] = day_weight * class_weight
                    if is_training:
                        dataset = tf.data.Dataset.from_tensor_slices((df_copy['path'].values, tf.keras.utils.to_categorical(df_copy['label'].values, num_classes=len(class_indices)), df_copy['sample_weight'].values))
                    else:
                        dataset = tf.data.Dataset.from_tensor_slices((df_copy['path'].values, tf.keras.utils.to_categorical(df_copy['label'].values, num_classes=len(class_indices))))
                    def parse_function(path, label, *weights):
                        img_str = tf.io.read_file(path); img = tf.io.decode_jpeg(img_str, channels=3); img = tf.image.resize(img, image_size)
                        # Remove rescaling from here as it's now part of the model's preprocessing
                        if is_training: return (img, label, weights[0])
                        else: return img, label
                    dataset = dataset.map(parse_function, num_parallel_calls=tf.data.AUTOTUNE)
                    if is_training: dataset = dataset.shuffle(buffer_size=1024)
                    dataset = dataset.batch(batch_size).prefetch(buffer_size=tf.data.AUTOTUNE); return dataset

                # CORRECTED build_model function
                def build_model():
                    IMAGE_SIZE = st.session_state.IMAGE_SIZE
                    inputs = tf.keras.Input(shape=(IMAGE_SIZE[0], IMAGE_SIZE[1], 3), name="input_layer")

                    data_augmentation = tf.keras.Sequential([
                        tf.keras.layers.RandomFlip("horizontal"),
                        tf.keras.layers.RandomRotation(0.1),
                        tf.keras.layers.RandomZoom(0.1),
                        tf.keras.layers.RandomContrast(0.1)
                    ], name="data_augmentation")
                    
                    base_model = MobileNetV2(
                        weights='imagenet',
                        include_top=False,
                        input_shape=(IMAGE_SIZE[0], IMAGE_SIZE[1], 3),
                        name="base_model"
                    )
                    base_model.trainable = False

                    x = data_augmentation(inputs)
                    x = tf.keras.applications.mobilenet_v2.preprocess_input(x)
                    x = base_model(x, training=False)
                    
                    x = GlobalAveragePooling2D()(x)
                    x = Dropout(0.3)(x)
                    predictions = Dense(len(st.session_state.model_classes), activation='softmax')(x)

                    model = Model(inputs=inputs, outputs=predictions)
                    model.compile(
                        optimizer=Adam(learning_rate=1e-4),
                        loss='categorical_crossentropy',
                        metrics=['accuracy']
                    )
                    return model

                if 'train_set_df' not in st.session_state:
                    X = train_df[['path', 'day']]; y = train_df['class']
                    X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
                    st.session_state.train_set_df = pd.concat([X_train, y_train], axis=1)
                    st.session_state.val_set_df = pd.concat([X_val, y_val], axis=1)
                    st.session_state.IMAGE_SIZE = (224, 224); st.session_state.model_classes = classes
                    st.session_state.class_indices = {cls: i for i, cls in enumerate(classes)}

                st.subheader("Training Configuration"); col1, col2, col3 = st.columns(3)
                epochs = col1.slider("Initial Epochs", 5, 50, 25)
                ft_epochs = col1.slider("Fine-Tuning Epochs", 1, 15, 5)
                batch_size = col2.select_slider("Batch Size", [16, 32, 64], 32)
                patience = col3.slider("Early Stopping Patience", 3, 10, 5)
                lr_patience = col3.slider("LR Reduction Patience", 2, 8, 3)
                
                with st.expander("⚖️ Applied Sample Weights", expanded=True):
                    class_counts = st.session_state.train_set_df['class'].value_counts()
                    class_weights = class_counts.sum() / (len(class_counts) * class_counts)
                    class_weights_dict = class_weights.to_dict(); st.write("Class Weights:"); st.dataframe(pd.DataFrame(class_weights).rename(columns={'count': 'Weight'}), use_container_width=True)
                    df_with_days = st.session_state.train_set_df[st.session_state.train_set_df['day'].notna()]
                    if not df_with_days.empty:
                        day_counts = df_with_days['day'].value_counts()
                        day_weights = day_counts.sum() / (len(day_counts) * day_counts)
                        day_weights_dict = day_weights.to_dict(); st.write("Day Weights:"); st.dataframe(pd.DataFrame(day_weights).rename(columns={'count': 'Weight'}), use_container_width=True)
                    else: day_weights_dict = {}

                if st.button("🚀 Run Training Comparison"):
                    with st.spinner("Preparing data loaders..."):
                        train_ds = create_dataset(st.session_state.train_set_df, st.session_state.class_indices, day_weights_dict, class_weights_dict, batch_size, st.session_state.IMAGE_SIZE, is_training=True)
                        val_ds = create_dataset(st.session_state.val_set_df, st.session_state.class_indices, day_weights_dict, class_weights_dict, batch_size, st.session_state.IMAGE_SIZE, is_training=False)
                    try:
                        # --- Model with Early Stopping & Fine-Tuning ---
                        with st.spinner(f"Stage 1: Training classifier with Early Stopping..."):
                            model_es = build_model()
                            callbacks_es = [EarlyStopping(monitor='val_loss', patience=patience, restore_best_weights=True), ReduceLROnPlateau(monitor='val_loss', patience=lr_patience)]
                            history_es_initial = model_es.fit(train_ds, validation_data=val_ds, epochs=epochs, callbacks=callbacks_es, verbose=1)
                            st.success(f"✅ Stage 1 training complete in {len(history_es_initial.history['val_loss'])} epochs!")

                        # --- Fine-Tuning Stage ---
                        with st.spinner(f"Stage 2: Fine-tuning last 20 layers for {ft_epochs} epochs..."):
                            base_model_layer = model_es.get_layer("base_model")
                            base_model_layer.trainable = True
                            for layer in base_model_layer.layers[:-20]:
                                layer.trainable = False
                            
                            model_es.compile(optimizer=Adam(learning_rate=1e-5), loss='categorical_crossentropy', metrics=['accuracy'])
                            
                            history_es_finetune = model_es.fit(train_ds, validation_data=val_ds, epochs=ft_epochs, callbacks=callbacks_es, verbose=1)
                            st.success(f"✅ Stage 2 fine-tuning complete!")

                        combined_history = history_es_initial.history
                        for key in combined_history.keys():
                            combined_history[key].extend(history_es_finetune.history[key])
                        
                        st.session_state.history_with_es = combined_history
                        st.session_state.model_with_es = model_es
                        
                        # --- Model without Early Stopping ---
                        with st.spinner(f"Training second model for full {epochs} epochs..."):
                            model_no_es = build_model()
                            history_no_es = model_no_es.fit(train_ds, validation_data=val_ds, epochs=epochs, callbacks=[], verbose=1)
                            st.session_state.history_without_es = history_no_es.history
                            st.session_state.model_without_es = model_no_es
                            st.success("✅ Full duration training complete!")
                        
                        st.rerun() 
                    except Exception as e: st.error(f"❌ An error occurred during training: {e}"); st.exception(e)
                
                if st.session_state.history_with_es and st.session_state.history_without_es:
                    st.subheader("📊 Training vs. Validation Performance")
                    history_with = st.session_state.history_with_es
                    history_without = st.session_state.history_without_es
                    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
                    axes[0].plot(history_with['accuracy'], label='Train Acc (ES+FT)', color='blue', linestyle='-'); axes[0].plot(history_with['val_accuracy'], label='Val Acc (ES+FT)', color='cyan', linestyle='-')
                    axes[0].plot(history_without['accuracy'], label='Train Acc (Full)', color='red', linestyle='--'); axes[0].plot(history_without['val_accuracy'], label='Val Acc (Full)', color='orange', linestyle='--')
                    axes[0].set_title('Accuracy'); axes[0].set_xlabel('Epoch'); axes[0].legend(); axes[0].grid(True, alpha=0.3)
                    axes[1].plot(history_with['loss'], label='Train Loss (ES+FT)', color='blue', linestyle='-'); axes[1].plot(history_with['val_loss'], label='Val Loss (ES+FT)', color='cyan', linestyle='-')
                    axes[1].plot(history_without['loss'], label='Train Loss (Full)', color='red', linestyle='--'); axes[1].plot(history_without['val_loss'], label='Val Loss (Full)', color='orange', linestyle='--')
                    axes[1].set_title('Loss'); axes[1].set_xlabel('Epoch'); axes[1].legend(); axes[1].grid(True, alpha=0.3)
                    st.pyplot(fig)

            # --- FINAL EVALUATION TAB ---
            if "🏆 Final Evaluation" in data_viz_tabs:
                with data_viz_tabs["🏆 Final Evaluation"]:
                    st.header("🏆 Final Model Evaluation on the Test Set")
                    st.info("This section evaluates the best trained model (with Early Stopping and Fine-Tuning) on the completely separate `test` dataset.")
                    
                    final_test_df = st.session_state.test_df_final
                    best_model = get_best_model()

                    test_ds = tf.data.Dataset.from_tensor_slices(final_test_df['path'].values)
                    def parse_test_function(path):
                        img_str = tf.io.read_file(path); img = tf.io.decode_jpeg(img_str, channels=3); img = tf.image.resize(img, st.session_state.IMAGE_SIZE); return img
                    test_ds = test_ds.map(parse_test_function, num_parallel_calls=tf.data.AUTOTUNE).batch(batch_size)
                    
                    with st.spinner("Making predictions on the test set..."):
                        predictions = best_model.predict(test_ds)
                        y_pred = np.argmax(predictions, axis=1)
                    
                    class_indices_map = st.session_state.class_indices
                    y_true = final_test_df['class'].map(class_indices_map).values
                    class_names = list(class_indices_map.keys())

                    st.subheader("Classification Report")
                    report = classification_report(y_true, y_pred, target_names=class_names, output_dict=True)
                    st.dataframe(pd.DataFrame(report).transpose())

                    st.subheader("Confusion Matrix")
                    cm = confusion_matrix(y_true, y_pred)
                    disp = ConfusionMatrixDisplay(confusion_matrix=cm, display_labels=class_names)
                    fig, ax = plt.subplots(figsize=(8, 6))
                    disp.plot(ax=ax, cmap='Blues', xticks_rotation='vertical')
                    st.pyplot(fig)

            with data_viz_tabs["Try it Yourself!"]:
                st.header("🎯 Try it Yourself!")
                best_model = get_best_model()
                if best_model is None: st.warning("⚠️ Please train a model first in the 'Model Training' tab.")
                else:
                    st.success(f"🤖 Model is ready! It can classify into: {', '.join(st.session_state.model_classes)}")
                    uploaded_image = st.file_uploader("Upload an embryo image to classify", type=['jpg', 'jpeg', 'png'], key="prediction_uploader")
                    if uploaded_image:
                        image = Image.open(uploaded_image); col1, col2 = st.columns(2)
                        with col1: st.image(image, caption=f"Uploaded: {uploaded_image.name}", use_container_width=True)
                        with col2:
                            st.subheader("🔍 Classification Results")
                            if st.button("Classify Image"):
                                with st.spinner("Analyzing image..."):
                                    p_class, p_conf, p_probs = predict_embryo_class(best_model, image, st.session_state.model_classes)
                                    st.success(f"**Predicted Class:** `{p_class}`"); st.info(f"**Confidence:** `{p_conf:.2%}`")
                                    prob_df = pd.DataFrame(list(p_probs.items()), columns=['Class', 'Probability']).sort_values('Probability', ascending=False)
                                    fig_prob, ax_prob = plt.subplots(); sns.barplot(x='Probability', y='Class', data=prob_df, ax=ax_prob, palette='viridis', hue='Class', legend=False); st.pyplot(fig_prob)
else:
    st.info("👋 Welcome! Please upload a .zip file to begin.")
