from flask import Flask, render_template, request, jsonify, session, redirect, url_for, g, flash, send_from_directory
from pathlib import Path
import sqlite3
import json
import numpy as np
from sklearn.preprocessing import MinMaxScaler
import random
from functools import wraps
from werkzeug.exceptions import HTTPException
import logging
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev'  # Change this to a secure key in production
app.config['DATABASE'] = 'recruitmentbuddy.db'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max request size

# Add debug logging for static files
@app.route('/static/<path:filename>')
def serve_static(filename):
    print(f"Serving static file: {filename}")
    return send_from_directory('static', filename)

def get_db():
    """Get database connection, storing it in g object"""
    if 'db' not in g:
        try:
            db_path = Path(app.config['DATABASE']).resolve()
            print(f"Connecting to database at: {db_path}")
            g.db = sqlite3.connect(str(db_path))
            g.db.row_factory = sqlite3.Row
            print("Database connection successful")
        except Exception as e:
            print(f"Database connection error: {e}")
            raise
    return g.db

@app.teardown_appcontext
def close_db(error):
    """Close database connection at the end of request"""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    """Initialize database with schema"""
    if not Path(app.config['DATABASE']).exists():
        with app.app_context():
            db = get_db()
            with app.open_resource('schema.sql', mode='r') as f:
                db.cursor().executescript(f.read())
            db.commit()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def validate_questionnaire_input(data):
    """Validate questionnaire input data"""
    if 'scores' not in data:
        raise ValueError("Missing 'scores' field in questionnaire data")
        
    required_fields = ['analytical_score', 'creative_score', 'social_score', 'technical_score']
    scores = data['scores']
    
    if not all(field in scores for field in required_fields):
        raise ValueError("Missing required fields in scores data")
    
    for field in required_fields:
        score = scores[field]
        if not isinstance(score, (int, float)) or score < 0 or score > 10:
            raise ValueError(f"Invalid score for {field}. Must be number between 0 and 10")

    return True

def get_personality_type(scores):
    """Calculate personality type based on questionnaire scores."""
    try:
        # Simple mapping of scores to personality dimensions
        # High analytical/technical -> Thinking (T), low -> Feeling (F)
        # High creative -> Intuitive (N), low -> Sensing (S)
        # High social -> Extravert (E), low -> Introvert (I)
        # High technical/creative -> Perceiving (P), low -> Judging (J)
        
        # Calculate each dimension
        ei_score = scores['social_score'] / 10.0
        sn_score = scores['creative_score'] / 10.0
        tf_score = (scores['analytical_score'] + scores['technical_score']) / 20.0
        jp_score = (scores['technical_score'] + scores['creative_score']) / 20.0
        
        # Determine type code
        type_code = ''
        type_code += 'E' if ei_score >= 0.5 else 'I'
        type_code += 'N' if sn_score >= 0.5 else 'S'
        type_code += 'T' if tf_score >= 0.5 else 'F'
        type_code += 'P' if jp_score >= 0.5 else 'J'
        
        # Calculate dimension scores
        dimension_scores = {
            'ei': ei_score,
            'sn': sn_score,
            'tf': tf_score,
            'jp': jp_score
        }
        
        # Return both the type code and dimension scores
        return {'code': type_code, 'scores': dimension_scores}, dimension_scores
    except Exception as e:
        logger.error(f"Error in personality type calculation: {str(e)}")
        raise

def get_personality_type_id(type_code):
    """Get or create a personality type record."""
    try:
        db = get_db()
        cursor = db.cursor()
        
        # Try to get existing type
        cursor.execute('SELECT id FROM personality_types WHERE code = ?', (type_code,))
        result = cursor.fetchone()
        
        if result:
            return result['id']
        else:
            # Create new type with basic info
            cursor.execute('''
                INSERT INTO personality_types (code, name, description)
                VALUES (?, ?, ?)
            ''', (type_code, f"Type {type_code}", "Personality type description"))
            db.commit()
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"Error getting/creating personality type: {str(e)}")
        raise

def calculate_major_matches(scores, personality_type_id=None):
    """Calculate match scores for all majors based on questionnaire scores and personality."""
    db = get_db()
    majors = db.execute('SELECT * FROM majors').fetchall()
    
    # Convert scores to 0-1 scale
    scaler = MinMaxScaler()
    score_values = np.array([[
        scores['analytical_score'],
        scores['creative_score'],
        scores['social_score'],
        scores['technical_score']
    ]])
    normalized_scores = scaler.fit_transform(score_values)[0]
    
    matches = []
    for major in majors:
        # Calculate match percentage for each dimension
        analytical_match = 1 - abs(normalized_scores[0] - major['analytical_weight'])
        creative_match = 1 - abs(normalized_scores[1] - major['creative_weight'])
        social_match = 1 - abs(normalized_scores[2] - major['social_weight'])
        technical_match = 1 - abs(normalized_scores[3] - major['technical_weight'])
        
        # Get personality match if available
        personality_match = 0.5  # Default to neutral
        if personality_type_id:
            personality_match_data = db.execute('''
                SELECT match_strength 
                FROM major_personality_matches 
                WHERE major_id = ? AND personality_type_id = ?
            ''', (major['id'], personality_type_id)).fetchone()
            
            if personality_match_data:
                personality_match = personality_match_data['match_strength']
        
        # Calculate overall match score (weighted average)
        match_score = (
            (analytical_match + creative_match + social_match + technical_match) / 4 * 0.7 +  # Skills weight
            personality_match * 0.3  # Personality weight
        )
        
        matches.append({
            'major_id': major['id'],
            'name': major['name'],
            'description': major['description'],
            'career_opportunities': major['career_opportunities'],
            'required_skills': major['required_skills'],
            'match_score': match_score,
            'analytical_match': analytical_match,
            'creative_match': creative_match,
            'social_match': social_match,
            'technical_match': technical_match,
            'personality_match': personality_match
        })
    
    # Sort matches by score in descending order
    matches.sort(key=lambda x: x['match_score'], reverse=True)
    return matches

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/questionnaire')
@login_required
def questionnaire():
    step = request.args.get('step', 1, type=int)
    questions = {
        1: {
            'text': 'How much do you enjoy analytical thinking and problem-solving?',
            'field': 'analytical',
            'progress': 25
        },
        2: {
            'text': 'How much do you enjoy creative and artistic activities?',
            'field': 'creative',
            'progress': 50
        },
        3: {
            'text': 'How much do you enjoy working with and helping others?',
            'field': 'social',
            'progress': 75
        },
        4: {
            'text': 'How comfortable are you with technical and hands-on work?',
            'field': 'technical',
            'progress': 100
        }
    }
    
    if step not in questions:
        return redirect(url_for('questionnaire'))
    
    return render_template('questionnaire.html', 
                         question=questions[step],
                         step=step,
                         total_steps=len(questions))

# Store questionnaire responses in session
@app.route('/questionnaire/next', methods=['POST'])
@login_required
def questionnaire_next():
    if not session.get('user_id'):
        return jsonify({'redirect': url_for('login')})
    
    data = request.get_json()
    step = data.get('step')
    
    if not session.get('questionnaire_responses'):
        session['questionnaire_responses'] = {}
    
    # Store the response for the current step
    if step == 1:
        session['questionnaire_responses']['analytical'] = data.get('analytical')
        return jsonify({'redirect': url_for('questionnaire', step=2)})
    elif step == 2:
        session['questionnaire_responses']['creative'] = data.get('creative')
        return jsonify({'redirect': url_for('questionnaire', step=3)})
    elif step == 3:
        session['questionnaire_responses']['social'] = data.get('social')
        return jsonify({'redirect': url_for('questionnaire', step=4)})
    elif step == 4:
        session['questionnaire_responses']['technical'] = data.get('technical')
        # Process final results
        return jsonify({'redirect': url_for('recommendations')})

@app.route('/submit_questionnaire', methods=['POST'])
@login_required
def submit_questionnaire():
    try:
        data = request.get_json()
        validate_questionnaire_input(data)
        
        # Get or create personality type
        primary_type, _ = get_personality_type(data['scores'])
        personality_type_id = get_personality_type_id(primary_type['code'])
        
        # Store questionnaire response
        db = get_db()
        cursor = db.cursor()
        
        cursor.execute('''
            INSERT INTO questionnaire_responses 
            (analytical_score, creative_score, social_score, technical_score, 
             personality_type_id, raw_responses)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            data['scores']['analytical_score'],
            data['scores']['creative_score'],
            data['scores']['social_score'],
            data['scores']['technical_score'],
            personality_type_id,
            json.dumps(data.get('responses', {}))  # Make responses optional
        ))
        response_id = cursor.lastrowid
        
        # Calculate and store major recommendations
        matches = calculate_major_matches(data['scores'], personality_type_id)
        
        for match in matches:
            cursor.execute('''
                INSERT INTO major_recommendations
                (response_id, major_id, match_score, analytical_match, 
                 creative_match, social_match, technical_match,
                 personality_match)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                response_id,
                match['major_id'],
                match['match_score'],
                match['analytical_match'],
                match['creative_match'],
                match['social_match'],
                match['technical_match'],
                match.get('personality_match', 0.5)  # Default to neutral if not calculated
            ))
        
        db.commit()
        session['scores'] = data['scores']  # Store scores in session
        return jsonify({'status': 'success', 'response_id': response_id})
    except ValueError as e:
        logger.error(f"Error in submitting questionnaire: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
    except Exception as e:
        logger.error(f"Error in submitting questionnaire: {str(e)}")
        return jsonify({'status': 'error', 'message': 'Internal server error'}), 500

@app.route('/recommendations')
@login_required
def recommendations():
    if not session.get('user_id'):
        return redirect(url_for('login'))
    
    if not session.get('questionnaire_responses'):
        return redirect(url_for('questionnaire'))
    
    # Get user's questionnaire responses
    user_scores = session.get('questionnaire_responses')
    
    # Get all majors from the database
    db = get_db()
    majors_data = db.execute('''
        SELECT m.id, m.name, m.description, m.careers, m.skills,
               m.analytical_weight, m.creative_weight, m.social_weight, m.technical_weight
        FROM majors m
    ''').fetchall()
    
    # Calculate match percentages and prepare major data
    majors = []
    for major in majors_data:
        # Calculate match percentage based on weights
        analytical_match = 100 - abs(float(user_scores['analytical']) - (float(major['analytical_weight']) * 10))
        creative_match = 100 - abs(float(user_scores['creative']) - (float(major['creative_weight']) * 10))
        social_match = 100 - abs(float(user_scores['social']) - (float(major['social_weight']) * 10))
        technical_match = 100 - abs(float(user_scores['technical']) - (float(major['technical_weight']) * 10))
        
        match_percentage = (analytical_match + creative_match + social_match + technical_match) / 4
        
        majors.append({
            'name': major['name'],
            'description': major['description'],
            'careers': major['careers'].split(','),
            'skills': major['skills'].split(','),
            'match_percentage': round(match_percentage),
            'weights': {
                'analytical': float(major['analytical_weight']),
                'creative': float(major['creative_weight']),
                'social': float(major['social_weight']),
                'technical': float(major['technical_weight'])
            }
        })
    
    # Sort majors by match percentage (highest first)
    majors.sort(key=lambda x: x['match_percentage'], reverse=True)
    
    # Take top 3 matches
    top_majors = majors[:3]
    
    return render_template('recommendations.html',
                         majors=top_majors,
                         user_scores=user_scores)

@app.route('/api/majors', methods=['GET'])
@login_required
def get_majors():
    """
    Get all majors
    ---
    responses:
      200:
        description: List of all majors
      500:
        description: Server error
    """
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            SELECT id, name, description, career_opportunities, 
                   required_skills, analytical_weight, creative_weight,
                   social_weight, technical_weight
            FROM majors
        ''')
        majors = [dict(row) for row in cursor.fetchall()]
        return jsonify(majors)
    except Exception as e:
        logger.error(f"Error fetching majors: {str(e)}")
        raise

@app.route('/api/personality-types', methods=['GET'])
@login_required
def get_personality_types():
    """
    Get all personality types
    ---
    responses:
      200:
        description: List of all personality types
      500:
        description: Server error
    """
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT * FROM personality_types')
        types = [dict(row) for row in cursor.fetchall()]
        return jsonify(types)
    except Exception as e:
        logger.error(f"Error fetching personality types: {str(e)}")
        raise

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Check if user is already logged in
    if 'user_id' in session:
        print("\n=== Already Logged In ===")
        print(f"User ID in session: {session['user_id']}")
        return redirect(url_for('questionnaire'))
        
    if request.method == 'POST':
        print("\n=== Login Form Data ===")
        print("Form data received:", dict(request.form))
        print("Raw form data:", request.get_data(as_text=True))
        
        email = request.form['email'].strip()
        password = request.form['password']
        error = None
        
        print(f"\n=== Login Attempt ===")
        print(f"Email provided: '{email}'")
        print(f"Email length: {len(email)}")
        print(f"Email characters (ASCII): {[ord(c) for c in email]}")
        
        try:
            # Direct database connection for debugging
            print("\nTrying direct database connection...")
            import sqlite3
            db_path = 'recruitmentbuddy.db'
            print(f"Database path: {db_path}")
            print(f"Database exists: {os.path.exists(db_path)}")
            
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Check if database has the users table
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()
            print("\nAll database tables:")
            for table in tables:
                print(f"- {table['name']}")
                # Show schema for each table
                cursor.execute(f"PRAGMA table_info({table['name']})")
                columns = cursor.fetchall()
                for col in columns:
                    print(f"  * {col['name']} ({col['type']})")
            
            # Get all users
            cursor.execute('SELECT * FROM users')
            all_users = cursor.fetchall()
            print("\nAll users in database:")
            for u in all_users:
                print(f"- ID: {u['id']}, Email: '{u['email']}', Name: {u['first_name']} {u['last_name']}")
            
            # Try to find our user
            cursor.execute('SELECT * FROM users WHERE email = ?', (email,))
            user = cursor.fetchone()
            
            if user:
                print(f"\nUser found:")
                print(f"- ID: {user['id']}")
                print(f"- Email: '{user['email']}'")
                print(f"- Name: {user['first_name']} {user['last_name']}")
                
                if check_password_hash(user['password'], password):
                    print("\n=== Login Successful ===")
                    session.clear()
                    session['user_id'] = user['id']
                    print(f"Set user_id in session to: {session['user_id']}")
                    flash('Successfully logged in!', 'success')
                    conn.close()
                    print("Redirecting to questionnaire...")
                    return redirect(url_for('questionnaire'))
                else:
                    error = 'Incorrect password.'
                    print(f"\nLogin failed: {error}")
            else:
                error = 'Invalid email address.'
                print(f"\nLogin failed: {error}")
            
            conn.close()
            flash(error, 'error')
            return render_template('login.html', error=error)
            
        except Exception as e:
            print(f"\nLogin error: {str(e)}")
            print("Exception type:", type(e))
            import traceback
            print("Traceback:", traceback.format_exc())
            flash('An error occurred during login.', 'error')
            return render_template('login.html', error='Server error')

    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        first_name = request.form['first_name']
        last_name = request.form['last_name']
        email = request.form['email']
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        error = None
        db = get_db()

        if not first_name:
            error = 'First name is required.'
        elif not last_name:
            error = 'Last name is required.'
        elif not email:
            error = 'Email is required.'
        elif not password:
            error = 'Password is required.'
        elif password != confirm_password:
            error = 'Passwords do not match.'
        elif db.execute(
            'SELECT id FROM users WHERE email = ?', (email,)
        ).fetchone() is not None:
            error = f'Email {email} is already registered.'

        if error is None:
            db.execute(
                'INSERT INTO users (first_name, last_name, email, password) VALUES (?, ?, ?, ?)',
                (first_name, last_name, email, generate_password_hash(password))
            )
            db.commit()
            # Log the user in automatically after signup
            user = db.execute(
                'SELECT * FROM users WHERE email = ?', (email,)
            ).fetchone()
            session.clear()
            session['user_id'] = user['id']
            flash('Account created successfully! Welcome to RecruitmentBuddy!', 'success')
            return redirect(url_for('index'))

        flash(error, 'error')
        return render_template('signup.html', error=error)

    return render_template('signup.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/forgot-password')
def forgot_password():
    # TODO: Implement password reset functionality
    return "Password reset functionality coming soon!"

# Add session debugging
@app.before_request
def debug_session():
    print("\n=== Session Debug ===")
    print("Session data:", dict(session))
    print("Request path:", request.path)
    print("Request method:", request.method)
    if request.method == 'POST':
        print("Form data:", dict(request.form))

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
