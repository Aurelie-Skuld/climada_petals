pipeline {
  agent any
  stages {
    stage('conda activate') {
      steps {
        sh 'conda activate climada_env'
      }
    }

    stage('lint') {
      parallel {
        stage('lint') {
          steps {
            sh 'make lint'
          }
        }

        stage('unit_test') {
          steps {
            sh 'make unit_test'
          }
        }

      }
    }

    stage('') {
      steps {
        sh 'conda deactivate'
      }
    }

  }
}